"""Policy layer. Every tool runs its inputs through here before touching the cluster."""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from typing import Any, Iterable

from .config import Settings
from .errors import SafetyError

log = logging.getLogger("es_mcp.safety")

# Endpoints that can destroy or overwrite data.
DESTRUCTIVE_OPS = {"restore_snapshot", "reindex", "put_mapping", "update_settings", "delete_by_query", "update_by_query"}


class Guard:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._audit = None
        if settings.audit_log_path:
            self._audit = open(settings.audit_log_path, "a", encoding="utf-8")  # noqa: SIM115

    # --- index policy -----------------------------------------------------
    def check_index(self, index: str | Iterable[str], *, write: bool = False) -> str:
        """Validate one index / pattern / comma list. Returns normalized string."""
        names = self._split(index)
        if not names:
            raise SafetyError("index is required")
        for name in names:
            if any(c in name for c in ('"', "\n", " ")):
                raise SafetyError(f"invalid index name: {name!r}")
            if not any(fnmatch.fnmatch(name, p) for p in self.s.index_allow):
                raise SafetyError(
                    f"index {name!r} not in allow-list {self.s.index_allow}. "
                    "Set ES_MCP_INDEX_ALLOW to widen access."
                )
            for deny in self.s.index_deny:
                if fnmatch.fnmatch(name, deny):
                    raise SafetyError(f"index {name!r} matches deny pattern {deny!r}")
            if write and name in ("*", "_all") or (write and name.endswith("*") and len(name) <= 2):
                raise SafetyError("refusing a write against a wildcard covering the whole cluster")
        return ",".join(names)

    @staticmethod
    def _split(index: str | Iterable[str]) -> list[str]:
        if isinstance(index, str):
            return [i.strip() for i in index.split(",") if i.strip()]
        return [str(i).strip() for i in index if str(i).strip()]

    # --- write policy -----------------------------------------------------
    def check_write(self, operation: str) -> None:
        if self.s.read_only:
            raise SafetyError(
                f"'{operation}' blocked: server is read-only. "
                "Restart with ES_MCP_READ_ONLY=false to enable writes."
            )
        if operation in DESTRUCTIVE_OPS and not self.s.allow_destructive:
            raise SafetyError(
                f"'{operation}' is destructive and blocked. "
                "Set ES_MCP_ALLOW_DESTRUCTIVE=true to enable it."
            )

    @staticmethod
    def check_confirm(confirm: bool, operation: str, target: str) -> None:
        if not confirm:
            raise SafetyError(
                f"'{operation}' on {target} needs confirm=true. "
                "Review the target and re-issue the call explicitly."
            )

    # --- query limits -----------------------------------------------------
    def clamp_size(self, size: int | None) -> int:
        if size is None:
            return self.s.default_size
        if size < 0:
            raise SafetyError("size must be >= 0")
        return min(size, self.s.max_result_size)

    def check_query_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """Reject shapes that reliably melt a cluster."""
        if not isinstance(body, dict):
            raise SafetyError("query body must be a JSON object")
        if "script" in json.dumps(body) and self.s.read_only:
            log.warning("query contains a script block")
        self._check_agg_size(body.get("aggs") or body.get("aggregations") or {}, depth=0)
        body = dict(body)
        body["size"] = self.clamp_size(body.get("size"))
        body.setdefault("timeout", self.s.search_timeout)
        if self.s.terminate_after:
            body.setdefault("terminate_after", self.s.terminate_after)
        frm = body.get("from", 0) or 0
        if frm + body["size"] > 10_000:
            raise SafetyError(
                "from+size exceeds the 10000 deep-paging limit; use search_after or PIT instead"
            )
        return body

    def _check_agg_size(self, aggs: dict[str, Any], depth: int) -> None:
        if depth > 5:
            raise SafetyError("aggregation nesting deeper than 5 levels is blocked")
        for name, spec in (aggs or {}).items():
            if not isinstance(spec, dict):
                continue
            for agg_type, cfg in spec.items():
                if agg_type in ("aggs", "aggregations"):
                    self._check_agg_size(cfg, depth + 1)
                    continue
                if isinstance(cfg, dict) and isinstance(cfg.get("size"), int):
                    if cfg["size"] > self.s.max_agg_buckets:
                        raise SafetyError(
                            f"aggregation {name!r} requests {cfg['size']} buckets, "
                            f"limit is {self.s.max_agg_buckets}"
                        )

    # --- audit ------------------------------------------------------------
    def audit(self, tool: str, args: dict[str, Any], outcome: str) -> None:
        record = {
            "ts": time.time(),
            "tool": tool,
            "outcome": outcome,
            "args": {k: v for k, v in args.items() if k not in ("password", "api_key")},
        }
        log.info("audit %s %s", tool, outcome)
        if self._audit:
            self._audit.write(json.dumps(record, default=str) + "\n")
            self._audit.flush()

    def close(self) -> None:
        if self._audit:
            self._audit.close()
