# elasticsearch-mcp

MCP server for Elasticsearch, built directly on the REST API (no `elasticsearch-py` dependency, so it also works against OpenSearch and managed clusters that only expose HTTP).

21 tools covering query, diagnostics, mapping, and cluster operations. Read-only by default; every write path is gated.

## Install

```bash
git clone <your-repo> elasticsearch-mcp && cd elasticsearch-mcp
uv sync            # or: pip install -e ".[dev]"
cp .env.example .env
pytest -q
```

## Register with Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "elasticsearch": {
      "command": "uv",
      "args": ["--directory", "/abs/path/elasticsearch-mcp", "run", "elasticsearch-mcp"],
      "env": {
        "ES_MCP_HOSTS": "https://es.internal:9200",
        "ES_MCP_API_KEY": "base64-encoded-api-key",
        "ES_MCP_INDEX_ALLOW": "logs-*,metrics-*",
        "ES_MCP_READ_ONLY": "true"
      }
    }
  }
}
```

Remote / shared deployment instead of stdio:

```bash
elasticsearch-mcp --transport streamable-http
```

## Tools

**Query**

| Tool | What it does |
|---|---|
| `run_query` | Execute Query DSL. Size capped, timeout injected, deep paging rejected, `search_after` supported |
| `generate_dsl` | Structured spec to valid DSL, validated against the index, returns a field catalog |
| `explain_query` | `_validate?rewrite=true` + `profile` timing breakdown + per-document `_explain` |
| `count_documents` | Match count without fetching hits |

**Diagnostics**

| Tool | What it does |
|---|---|
| `cluster_health` | Status, shard counts, pending tasks, optional per-node heap/disk/CPU/rejections |
| `index_health` | Per index: docs, store, segments, avg query and index latency, merges |
| `shard_allocation` | `_cat/shards` plus `_cluster/allocation/explain` decider reasons for unassigned shards |
| `find_slow_queries` | Indices ranked by avg query latency, running search tasks, thread-pool rejections, slowlog thresholds |

**Mapping**

| Tool | What it does |
|---|---|
| `list_indices` | Indices and aliases with doc counts and size |
| `get_mapping` | Flattened `field path -> type` catalog including multi-fields |
| `analyze_text` | Token output of an analyzer, for debugging zero-hit match queries |
| `put_mapping` | Additive mapping changes (write-gated) |

**Operations**

| Tool | What it does |
|---|---|
| `list_snapshot_repositories`, `list_snapshots`, `snapshot_status` | Snapshot inventory and progress |
| `create_snapshot` | Snapshot selected indices, async by default |
| `restore_snapshot` | Restore, with pre-flight check for existing open indices and rename support |
| `reindex` | Async reindex with query, pipeline, script, slicing, throttling; returns a task id |
| `get_task`, `cancel_task` | Poll or kill long-running tasks |
| `cluster_info` | Version, distribution, and the active safety policy |

## Configuration

All variables use the `ES_MCP_` prefix, read from the environment or `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `HOSTS` | `http://localhost:9200` | Comma separated; failover across them |
| `API_KEY` / `USERNAME`+`PASSWORD` / `BEARER_TOKEN` | – | Pick one auth mode |
| `VERIFY_CERTS`, `CA_CERTS`, `CLIENT_CERT`, `CLIENT_KEY` | `true` | TLS |
| `REQUEST_TIMEOUT`, `CONNECT_TIMEOUT`, `MAX_RETRIES` | `30`, `10`, `3` | Retries cover 429/502/503/504 and connect errors, with jittered backoff and `Retry-After` |
| `READ_ONLY` | `true` | Master switch for all write tools |
| `ALLOW_DESTRUCTIVE` | `false` | Second gate for restore, reindex, put_mapping |
| `INDEX_ALLOW` | `*` | Glob allow-list |
| `INDEX_DENY` | `.*,security-*` | Glob deny-list; deny wins |
| `DEFAULT_SIZE`, `MAX_RESULT_SIZE` | `10`, `200` | Hit caps |
| `MAX_AGG_BUCKETS` | `1000` | Rejects bucket explosions |
| `SEARCH_TIMEOUT`, `TERMINATE_AFTER` | `30s`, unset | Per-query guards |
| `MAX_RESPONSE_CHARS`, `MAX_SOURCE_CHARS` | `60000`, `2000` | Token control |
| `AUDIT_LOG_PATH` | unset | JSONL record of every tool call and outcome |
| `LOG_LEVEL` | `INFO` | Logs go to stderr, never stdout (stdio transport) |

## Safety model

Four independent layers, all failing closed:

1. **Index policy** — allow-list and deny-list checked on every call; deny wins; wildcard writes across the whole cluster refused.
2. **Read-only** — mutating tools refuse unless `READ_ONLY=false`.
3. **Destructive gate** — restore, reindex, and put_mapping additionally need `ALLOW_DESTRUCTIVE=true` and an explicit `confirm=true` argument in the call itself.
4. **Query limits** — size clamp, agg bucket cap, agg nesting depth cap, deep-paging rejection, injected search timeout.

Tools return errors as readable text (`ERROR (run_query): ...`) rather than raising, so the model can correct itself instead of stalling.

Recommended production posture: a dedicated ES API key with `read` on exactly the allowed indices, `READ_ONLY=true`, and a separate write-enabled instance only if you actually need reindex/restore from the assistant.

## Extending

Add a module under `src/es_mcp/tools/`, expose `register(server, ctx)`, wire it in `server.build_server`. Use `ctx.guard` for policy checks and `ctx.render` for output so limits apply automatically.
