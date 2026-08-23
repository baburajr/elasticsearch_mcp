from __future__ import annotations

import json
from typing import Any

from ..errors import SafetyError
from ..formatting import flatten_mapping, parse_cat
from ._base import ToolContext, guarded


def register(server: Any, ctx: ToolContext) -> None:
    ro = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}

    @server.tool(
        name="list_indices",
        description="List indices, data streams and aliases matching a pattern, with doc counts and store size.",
        annotations=ro,
    )
    @guarded(ctx, "list_indices")
    async def list_indices(pattern: str = "*", include_aliases: bool = True) -> str:
        idx = ctx.guard.check_index(pattern)
        indices = parse_cat(
            await ctx.client.get(
                f"/_cat/indices/{idx}",
                params={
                    "format": "json",
                    "bytes": "b",
                    "h": "health,status,index,pri,rep,docs.count,store.size",
                    "s": "index",
                    "expand_wildcards": "open",
                },
            )
        )
        out: dict[str, Any] = {"count": len(indices), "indices": indices[:300]}
        if include_aliases:
            out["aliases"] = parse_cat(
                await ctx.client.get(
                    "/_cat/aliases", params={"format": "json", "h": "alias,index,is_write_index"}
                )
            )[:200]
        return ctx.render(out)

    @server.tool(
        name="get_mapping",
        description=(
            "Get the mapping for an index. Returns a flattened field catalog (field path -> type, including "
            "multi-fields like .keyword) which is what you need to write correct queries, plus dynamic "
            "templates and total field count. Set raw=true for the untouched mapping JSON."
        ),
        annotations=ro,
    )
    @guarded(ctx, "get_mapping")
    async def get_mapping(index: str, raw: bool = False, field_filter: str | None = None) -> str:
        idx = ctx.guard.check_index(index)
        mapping = await ctx.client.get(f"/{idx}/_mapping", params={"ignore_unavailable": True})
        if raw:
            return ctx.render(mapping)
        out: dict[str, Any] = {}
        for name, spec in mapping.items():
            m = spec.get("mappings") or {}
            fields = flatten_mapping(m.get("properties", {}))
            if field_filter:
                fields = {k: v for k, v in fields.items() if field_filter.lower() in k.lower()}
            out[name] = {
                "field_count": len(fields),
                "dynamic": m.get("dynamic", "true"),
                "fields": dict(sorted(fields.items())[:300]),
            }
            if m.get("dynamic_templates"):
                out[name]["dynamic_templates"] = m["dynamic_templates"]
        return ctx.render(out)

    @server.tool(
        name="analyze_text",
        description=(
            "Run text through an analyzer and see the resulting tokens. Use it when a match query returns "
            "nothing: it shows exactly how the field's analyzer tokenizes the indexed text vs your search term."
        ),
        annotations=ro,
    )
    @guarded(ctx, "analyze_text")
    async def analyze_text(
        text: str, index: str | None = None, analyzer: str | None = None, field: str | None = None
    ) -> str:
        body: dict[str, Any] = {"text": text}
        if analyzer:
            body["analyzer"] = analyzer
        if field:
            body["field"] = field
        path = "/_analyze"
        if index:
            path = f"/{ctx.guard.check_index(index)}/_analyze"
        resp = await ctx.client.post(path, body=body)
        return ctx.render(
            {
                "tokens": [
                    {"token": t.get("token"), "type": t.get("type"), "position": t.get("position")}
                    for t in resp.get("tokens", [])
                ]
            }
        )

    @server.tool(
        name="put_mapping",
        description=(
            "Add new fields to an existing index mapping. Only additive changes are possible in Elasticsearch; "
            "changing an existing field type requires reindex. Blocked unless the server runs with writes "
            "enabled, and requires confirm=true."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    )
    @guarded(ctx, "put_mapping")
    async def put_mapping(index: str, properties: str | dict, confirm: bool = False) -> str:
        ctx.guard.check_write("put_mapping")
        idx = ctx.guard.check_index(index, write=True)
        ctx.guard.check_confirm(confirm, "put_mapping", idx)
        props = properties if isinstance(properties, dict) else json.loads(properties)
        if not isinstance(props, dict) or not props:
            raise SafetyError("properties must be a non-empty JSON object of field definitions")
        body = props if "properties" in props else {"properties": props}
        resp = await ctx.client.put(f"/{idx}/_mapping", body=body)
        return ctx.render({"acknowledged": resp.get("acknowledged"), "index": idx, "added": list(body["properties"])})
