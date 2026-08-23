from __future__ import annotations

import logging
import sys
from typing import Any

from .client import ESClient
from .config import Settings, load_settings
from .safety import Guard
from .tools import health, ingest, mapping, ops, search, sql
from .tools._base import ToolContext

log = logging.getLogger("es_mcp")

# --- SDK compatibility ----------------------------------------------------
# MCP Python SDK >= 2 exposes MCPServer; 1.x exposes FastMCP. Both provide
# .tool() and .run(transport=...).
try:  # pragma: no cover - depends on installed SDK
    from mcp.server import MCPServer as _Server  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]


INSTRUCTIONS = """\
Elasticsearch operations server, backed by the ES REST API.

Recommended flow:
1. list_indices / get_mapping    - discover indices and exact field names first.
2. generate_dsl                  - turn intent into validated Query DSL.
3. run_query or count_documents  - execute it.
4. explain_query                 - when results are wrong or slow.
5. cluster_health / index_health / shard_allocation / find_slow_queries - diagnostics.
6. create_snapshot / restore_snapshot / reindex - write path, gated by config.

Rules:
- Never guess field names; read the mapping. A text field usually needs
  field.keyword for term queries, aggregations and sorting.
- Result size, aggregation buckets and deep paging are capped by server policy.
- Write tools fail closed. If a call is refused, report the reason instead of
  retrying with different parameters.
"""


def build_server(settings: Settings | None = None) -> Any:
    settings = settings or load_settings()
    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,  # stdout is the MCP stdio channel; never log there
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    client = ESClient(settings)
    guard = Guard(settings)
    ctx = ToolContext(client=client, guard=guard, settings=settings)

    server = _Server(
        name=settings.server_name,
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )

    search.register(server, ctx)
    health.register(server, ctx)
    mapping.register(server, ctx)
    ops.register(server, ctx)
    sql.register(server, ctx)
    ingest.register(server, ctx)

    @server.tool(
        name="cluster_info",
        description="Cluster name, version, distribution, and the safety policy this server is running under.",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def cluster_info() -> str:
        try:
            info = await client.ping()
        except Exception as exc:  # noqa: BLE001
            info = {"error": str(exc)}
        return ctx.render(
            {
                "cluster": info,
                "hosts": settings.hosts,
                "policy": {
                    "read_only": settings.read_only,
                    "allow_destructive": settings.allow_destructive,
                    "index_allow": settings.index_allow,
                    "index_deny": settings.index_deny,
                    "max_result_size": settings.max_result_size,
                    "max_agg_buckets": settings.max_agg_buckets,
                    "search_timeout": settings.search_timeout,
                },
            }
        )

    log.info(
        "elasticsearch-mcp ready hosts=%s read_only=%s destructive=%s",
        settings.hosts,
        settings.read_only,
        settings.allow_destructive,
    )
    return server
