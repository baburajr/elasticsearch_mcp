from __future__ import annotations

import re
from typing import Any

from ..errors import SafetyError
from ._base import ToolContext, guarded

# Only read-shaped SQL. Elasticsearch SQL is read-only anyway, but reject
# anything that looks like a mutation so a typo can never reach a write path.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|MERGE|GRANT|REVOKE)\b", re.IGNORECASE
)


def _first_table(sql: str) -> str | None:
    m = re.search(r"\bFROM\s+\"?([\w\-.*,]+)\"?", sql, re.IGNORECASE)
    return m.group(1) if m else None


def register(server: Any, ctx: ToolContext) -> None:
    ro = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}

    @server.tool(
        name="sql_query",
        description=(
            "Run an Elasticsearch SQL query via the _sql API. Good for quick aggregate/filter questions "
            "without hand-writing Query DSL, e.g. "
            "SELECT status, COUNT(*) FROM \"logs-*\" WHERE code >= 500 GROUP BY status. Only SELECT is "
            "allowed. The index in FROM is checked against the allow/deny policy. Row count is capped by "
            "fetch_size (server result-size limit applies). Returns columns and rows; if there are more "
            "rows a cursor is returned to pass back as the cursor argument."
        ),
        annotations=ro,
    )
    @guarded(ctx, "sql_query")
    async def sql_query(
        query: str | None = None,
        cursor: str | None = None,
        fetch_size: int | None = None,
    ) -> str:
        if cursor:
            resp = await ctx.client.post("/_sql", params={"format": "json"}, body={"cursor": cursor})
            return ctx.render(
                {"rows": resp.get("rows", []), "cursor": resp.get("cursor")}
            )
        if not query:
            raise SafetyError("provide either query or cursor")
        if _FORBIDDEN.search(query):
            raise SafetyError("only SELECT statements are allowed")
        table = _first_table(query)
        if table:
            ctx.guard.check_index(table)  # enforce allow/deny on the FROM target

        size = ctx.guard.clamp_size(fetch_size)
        resp = await ctx.client.post(
            "/_sql",
            params={"format": "json"},
            body={"query": query, "fetch_size": size},
        )
        return ctx.render(
            {
                "columns": resp.get("columns", []),
                "rows": resp.get("rows", []),
                "cursor": resp.get("cursor"),
                "hint": "pass cursor back to fetch the next page" if resp.get("cursor") else None,
            }
        )

    @server.tool(
        name="sql_translate",
        description=(
            "Translate an Elasticsearch SQL SELECT into the equivalent native Query DSL via _sql/translate, "
            "without running it. Use this to learn the DSL for a query, then hand the DSL to run_query for "
            "full control (search_after, routing, profiling)."
        ),
        annotations=ro,
    )
    @guarded(ctx, "sql_translate")
    async def sql_translate(query: str) -> str:
        if _FORBIDDEN.search(query):
            raise SafetyError("only SELECT statements are allowed")
        table = _first_table(query)
        if table:
            ctx.guard.check_index(table)
        resp = await ctx.client.post("/_sql/translate", body={"query": query})
        return ctx.render(resp)
