"""Exception types shared across the server.

All three inherit from ``ESMCPError`` so a caller can catch the base. Tool entry
points convert these to readable ``ERROR (tool): ...`` text (see ``tools._base``)
rather than letting them propagate, so the model can self-correct.
"""

from __future__ import annotations

from typing import Any


class ESMCPError(Exception):
    """Base for every error this package raises."""


class SafetyError(ESMCPError):
    """A policy gate refused the call: allow/deny list, read-only, confirm, limits."""


class TransportError(ESMCPError):
    """Network-level failure (connect refused, timeout) after retries are exhausted."""


class ElasticsearchError(ESMCPError):
    """A 4xx/5xx response from Elasticsearch. Carries the parsed error body."""

    def __init__(self, status: int, payload: Any, method: str, path: str) -> None:
        self.status = status
        self.payload = payload
        self.method = method
        self.path = path
        super().__init__(self._format())

    def _format(self) -> str:
        reason = self._reason(self.payload)
        return f"{self.method} {self.path} -> {self.status}: {reason}"

    @staticmethod
    def _reason(payload: Any) -> str:
        if isinstance(payload, dict):
            err = payload.get("error", payload)
            if isinstance(err, dict):
                root = err.get("root_cause")
                if isinstance(root, list) and root:
                    first = root[0]
                    if isinstance(first, dict) and first.get("reason"):
                        return str(first["reason"])
                if err.get("reason"):
                    return str(err["reason"])
                if err.get("type"):
                    return str(err["type"])
            if isinstance(err, str):
                return err
        return str(payload)[:500]
