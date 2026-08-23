"""Output shaping helpers: flatten mappings, normalize _cat JSON, summarize hits.

Kept separate from the tools so every tool renders results the same way and
size limits are enforced in one place (see ``tools._base.ToolContext.render``).
"""

from __future__ import annotations

from typing import Any

from .config import Settings


def flatten_mapping(properties: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Turn a nested ES mapping ``properties`` block into ``{field.path: type}``.

    Multi-fields (``fields``) become ``parent.subfield`` entries, and ``object`` /
    nested types recurse. The parent of a multi-field keeps its own type, so a
    ``text`` field with a ``.keyword`` shows up as both ``svc`` -> ``text`` and
    ``svc.keyword`` -> ``keyword``.
    """
    out: dict[str, str] = {}
    for name, spec in (properties or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{name}"
        ftype = spec.get("type")
        if "properties" in spec:  # object / nested container
            if ftype:
                out[path] = str(ftype)
            out.update(flatten_mapping(spec["properties"], prefix=f"{path}."))
        elif ftype:
            out[path] = str(ftype)
        else:
            out[path] = "object"
        for sub_name, sub_spec in (spec.get("fields") or {}).items():
            if isinstance(sub_spec, dict) and sub_spec.get("type"):
                out[f"{path}.{sub_name}"] = str(sub_spec["type"])
    return out


def parse_cat(rows: Any) -> list[dict[str, Any]]:
    """Normalize a ``_cat/*?format=json`` response to a list of plain dicts."""
    if not isinstance(rows, list):
        return []
    return [dict(r) for r in rows if isinstance(r, dict)]


def _truncate_source(source: Any, limit: int) -> Any:
    if limit <= 0:
        return source
    import json

    text = json.dumps(source, default=str)
    if len(text) <= limit:
        return source
    return {"_truncated": True, "_preview": text[:limit]}


def summarize_search(resp: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Compact a raw ``_search`` response into hits + timing + shard/agg summary."""
    hits_block = resp.get("hits", {}) if isinstance(resp, dict) else {}
    total = hits_block.get("total")
    total_hits = total.get("value") if isinstance(total, dict) else total

    limit = settings.max_source_chars
    hits = []
    for h in hits_block.get("hits", []):
        hits.append(
            {
                "_index": h.get("_index"),
                "_id": h.get("_id"),
                "_score": h.get("_score"),
                "_source": _truncate_source(h.get("_source"), limit),
                **({"sort": h["sort"]} if "sort" in h else {}),
                **({"highlight": h["highlight"]} if "highlight" in h else {}),
            }
        )

    out: dict[str, Any] = {
        "total_hits": total_hits,
        "total_relation": total.get("relation") if isinstance(total, dict) else None,
        "max_score": hits_block.get("max_score"),
        "took_ms": resp.get("took"),
        "timed_out": resp.get("timed_out"),
        "shards": resp.get("_shards"),
        "hits": hits,
    }
    if resp.get("aggregations"):
        out["aggregations"] = resp["aggregations"]
    # last hit's sort keys are the search_after cursor for the next page
    if hits and "sort" in hits[-1]:
        out["next_search_after"] = hits[-1]["sort"]
    return out
