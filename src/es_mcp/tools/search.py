from __future__ import annotations

import json
from typing import Any

from ..dsl import QuerySpec, build_query
from ..errors import SafetyError
from ..formatting import flatten_mapping, summarize_search
from ._base import ToolContext, guarded


def _as_dict(body: str | dict[str, Any] | None) -> dict[str, Any]:
    if body is None:
        return {"query": {"match_all": {}}}
    if isinstance(body, dict):
        return body
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SafetyError(f"query is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SafetyError("query JSON must be an object")
    return parsed


def register(server: Any, ctx: ToolContext) -> None:
    ro = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}

    @server.tool(
        name="run_query",
        description=(
            "Execute an Elasticsearch search against an index or alias. Accepts a full Query DSL body "
            "(JSON string or object). Size is capped by server policy, a search timeout is injected, and "
            "deep paging is rejected. Returns hits, aggregations, timing and shard stats. "
            "Use search_after for pagination beyond 10k."
        ),
        annotations=ro,
    )
    @guarded(ctx, "run_query")
    async def run_query(
        index: str,
        query: str | dict | None = None,
        size: int | None = None,
        from_: int = 0,
        sort: list[str] | None = None,
        source_includes: list[str] | None = None,
        search_after: list[Any] | None = None,
        routing: str | None = None,
        preference: str | None = None,
        profile: bool = False,
    ) -> str:
        idx = ctx.guard.check_index(index)
        body = _as_dict(query)
        if size is not None:
            body["size"] = size
        if from_:
            body["from"] = from_
        if sort:
            body["sort"] = [{s.split(":")[0]: {"order": s.split(":")[1]}} if ":" in s else s for s in sort]
        if source_includes:
            body["_source"] = source_includes
        if search_after:
            body["search_after"] = search_after
            body.pop("from", None)
            if "sort" not in body:
                raise SafetyError("search_after requires an explicit sort")
        if profile:
            body["profile"] = True

        body = ctx.guard.check_query_body(body)
        resp = await ctx.client.post(
            f"/{idx}/_search",
            params={"routing": routing, "preference": preference, "ignore_unavailable": True},
            body=body,
        )
        out = summarize_search(resp, ctx.settings)
        if profile and resp.get("profile"):
            shards = resp["profile"].get("shards", [])[:1]
            out["profile_first_shard"] = shards
        return ctx.render(out)

    @server.tool(
        name="count_documents",
        description="Count matching documents without returning hits. Cheap way to size a query before running it.",
        annotations=ro,
    )
    @guarded(ctx, "count_documents")
    async def count_documents(index: str, query: str | dict | None = None) -> str:
        idx = ctx.guard.check_index(index)
        body = _as_dict(query)
        body.pop("size", None)
        body.pop("sort", None)
        body.pop("aggs", None)
        body.pop("track_total_hits", None)
        resp = await ctx.client.post(f"/{idx}/_count", body=body, params={"ignore_unavailable": True})
        return ctx.render(resp)

    @server.tool(
        name="generate_dsl",
        description=(
            "Build Elasticsearch Query DSL from a structured spec, then validate it against the real index. "
            "Supply spec as JSON with any of: text, text_fields, filters [{field, op, value}], must_not, should, "
            "time_field/time_from/time_to, sort, size, aggs [{name, type, field, size, interval}], "
            "highlight_fields, source_includes. Ops: eq, neq, in, not_in, gt, gte, lt, lte, exists, missing, "
            "prefix, wildcard, match, match_phrase. Returns the generated DSL plus validation result and a "
            "field catalog so you can correct field names before running it."
        ),
        annotations=ro,
    )
    @guarded(ctx, "generate_dsl")
    async def generate_dsl(index: str, spec: str | dict, validate: bool = True) -> str:
        idx = ctx.guard.check_index(index)
        raw = spec if isinstance(spec, dict) else json.loads(spec)
        parsed = QuerySpec.model_validate(raw)
        body = build_query(parsed)
        out: dict[str, Any] = {"index": idx, "dsl": body}

        if validate:
            try:
                v = await ctx.client.post(
                    f"/{idx}/_validate/query",
                    params={"explain": True, "rewrite": True, "ignore_unavailable": True},
                    body={"query": body["query"]},
                )
                out["valid"] = v.get("valid")
                out["explanations"] = v.get("explanations", [])[:3]
            except Exception as exc:  # noqa: BLE001
                out["validation_error"] = str(exc)

        try:
            mapping = await ctx.client.get(f"/{idx}/_mapping", params={"ignore_unavailable": True})
            fields: dict[str, str] = {}
            for spec_ in mapping.values():
                fields.update(flatten_mapping((spec_.get("mappings") or {}).get("properties", {})))
            out["field_catalog"] = dict(sorted(fields.items())[:200])
        except Exception:  # noqa: BLE001
            pass
        out["next_step"] = "pass dsl to run_query"
        return ctx.render(out)

    @server.tool(
        name="explain_query",
        description=(
            "Explain a query three ways: (1) _validate/query?rewrite=true shows how ES rewrites it and why it "
            "may be invalid, (2) profile=true gives per-component timing to find the slow clause, "
            "(3) if doc_id is given, _explain shows why that document matched or did not, with scoring detail."
        ),
        annotations=ro,
    )
    @guarded(ctx, "explain_query")
    async def explain_query(
        index: str,
        query: str | dict,
        doc_id: str | None = None,
        profile: bool = True,
    ) -> str:
        idx = ctx.guard.check_index(index)
        body = _as_dict(query)
        inner = body.get("query", body)
        out: dict[str, Any] = {}

        validation = await ctx.client.post(
            f"/{idx}/_validate/query",
            params={"explain": True, "rewrite": True, "ignore_unavailable": True},
            body={"query": inner},
        )
        out["valid"] = validation.get("valid")
        out["rewritten"] = validation.get("explanations", [])[:3]

        if profile and validation.get("valid"):
            prof_body = ctx.guard.check_query_body({"query": inner, "size": 0, "profile": True})
            presp = await ctx.client.post(f"/{idx}/_search", body=prof_body)
            shards = (presp.get("profile") or {}).get("shards", [])
            if shards:
                searches = shards[0].get("searches", [{}])[0]
                out["profile"] = {
                    "shard": shards[0].get("id"),
                    "rewrite_time_ns": searches.get("rewrite_time"),
                    "query_breakdown": [
                        {
                            "type": q.get("type"),
                            "description": q.get("description", "")[:200],
                            "time_ns": q.get("time_in_nanos"),
                        }
                        for q in searches.get("query", [])[:5]
                    ],
                    "collector": searches.get("collector", [])[:2],
                    "aggregations_ns": sum(
                        a.get("time_in_nanos", 0) for a in shards[0].get("aggregations", [])
                    ),
                }
            out["took_ms"] = presp.get("took")

        if doc_id:
            expl = await ctx.client.post(f"/{idx}/_explain/{doc_id}", body={"query": inner})
            out["document_explanation"] = {
                "matched": expl.get("matched"),
                "explanation": expl.get("explanation"),
            }
        return ctx.render(out)

    # ---------------- point-in-time paging --------------------------------
    @server.tool(
        name="open_pit",
        description=(
            "Open a point-in-time (PIT) against an index and return its pit_id. A PIT freezes the data view "
            "so you can page through a large result set consistently with paged_search, past the 10000 "
            "deep-paging limit. Always close it with close_pit when done; keep_alive controls how long ES "
            "holds it open."
        ),
        annotations=ro,
    )
    @guarded(ctx, "open_pit")
    async def open_pit(index: str, keep_alive: str = "1m") -> str:
        idx = ctx.guard.check_index(index)
        resp = await ctx.client.post(f"/{idx}/_pit", params={"keep_alive": keep_alive})
        return ctx.render({"pit_id": resp.get("id"), "keep_alive": keep_alive})

    @server.tool(
        name="close_pit",
        description="Close a point-in-time by its pit_id to free cluster resources. Call after paging finishes.",
        annotations=ro,
    )
    @guarded(ctx, "close_pit")
    async def close_pit(pit_id: str) -> str:
        resp = await ctx.client.delete("/_pit", body={"id": pit_id})
        return ctx.render(resp)

    @server.tool(
        name="paged_search",
        description=(
            "Page through a large result set beyond the 10000 deep-paging limit using a point-in-time and "
            "search_after. First call: open_pit, then call this with the pit_id and a query; it returns one "
            "page plus next_search_after and the (possibly refreshed) pit_id. Pass both back on the next call "
            "to get the following page. A sort is required and an implicit _shard_doc tiebreak is added for "
            "stable ordering. No index argument: the PIT already binds the index. Close with close_pit at the "
            "end."
        ),
        annotations=ro,
    )
    @guarded(ctx, "paged_search")
    async def paged_search(
        pit_id: str,
        query: str | dict | None = None,
        sort: list[str] | None = None,
        search_after: list[Any] | None = None,
        size: int | None = None,
        source_includes: list[str] | None = None,
        keep_alive: str = "1m",
    ) -> str:
        body = _as_dict(query)
        sort = sort or ["_shard_doc:asc"]
        sort_clause = [
            {s.split(":")[0]: {"order": s.split(":")[1]}} if ":" in s else s for s in sort
        ]
        # _shard_doc tiebreak guarantees a total order across the PIT
        if not any("_shard_doc" in str(s) for s in sort):
            sort_clause.append({"_shard_doc": {"order": "asc"}})
        body["sort"] = sort_clause
        body["pit"] = {"id": pit_id, "keep_alive": keep_alive}
        if search_after:
            body["search_after"] = search_after
        body.pop("from", None)  # from is incompatible with search_after
        if source_includes:
            body["_source"] = source_includes
        if size is not None:
            body["size"] = size

        body = ctx.guard.check_query_body(body)
        # _search with a pit must not carry an index in the path
        resp = await ctx.client.post("/_search", body=body)
        out = summarize_search(resp, ctx.settings)
        out["pit_id"] = resp.get("pit_id", pit_id)  # ES may return a refreshed id
        if not out.get("hits"):
            out["done"] = True
            out["hint"] = "no more hits; call close_pit(pit_id)"
        return ctx.render(out)
