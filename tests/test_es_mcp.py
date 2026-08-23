from __future__ import annotations

import json

import httpx
import pytest

from es_mcp.client import ESClient
from es_mcp.config import Settings
from es_mcp.dsl import QuerySpec, build_query
from es_mcp.errors import ElasticsearchError, SafetyError, TransportError
from es_mcp.safety import Guard
from es_mcp.tools._base import ToolContext
from es_mcp.tools import search as search_tools
from es_mcp.tools import ops as ops_tools
from es_mcp.tools import sql as sql_tools
from es_mcp.tools import ingest as ingest_tools
from es_mcp.tools import health as health_tools


def make_settings(**kw) -> Settings:
    base = dict(hosts=["http://es:9200"], read_only=True, max_retries=1, backoff_base=0.0)
    base.update(kw)
    return Settings(**base)


class FakeServer:
    """Stands in for MCPServer/FastMCP: captures tools registered by name."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name=None, description=None, annotations=None, **_):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return deco


# --------------------------- safety ---------------------------------------
def test_deny_pattern_blocks_system_indices():
    g = Guard(make_settings())
    with pytest.raises(SafetyError, match="deny pattern"):
        g.check_index(".security-7")


def test_allow_list_restricts_access():
    g = Guard(make_settings(index_allow=["logs-*"]))
    assert g.check_index("logs-app-2026") == "logs-app-2026"
    with pytest.raises(SafetyError, match="allow-list"):
        g.check_index("orders")


def test_read_only_blocks_writes():
    g = Guard(make_settings())
    with pytest.raises(SafetyError, match="read-only"):
        g.check_write("reindex")


def test_destructive_gate_independent_of_read_only():
    g = Guard(make_settings(read_only=False, allow_destructive=False))
    g.check_write("create_snapshot")  # allowed
    with pytest.raises(SafetyError, match="destructive"):
        g.check_write("reindex")


def test_size_clamped_and_deep_paging_blocked():
    g = Guard(make_settings(max_result_size=50))
    body = g.check_query_body({"query": {"match_all": {}}, "size": 5000})
    assert body["size"] == 50
    assert body["timeout"] == "30s"
    with pytest.raises(SafetyError, match="deep-paging"):
        g.check_query_body({"query": {}, "size": 50, "from": 10_000})


def test_agg_bucket_cap():
    g = Guard(make_settings(max_agg_buckets=100))
    with pytest.raises(SafetyError, match="buckets"):
        g.check_query_body({"aggs": {"by_user": {"terms": {"field": "user", "size": 5000}}}})


def test_confirm_required():
    with pytest.raises(SafetyError, match="confirm=true"):
        Guard.check_confirm(False, "reindex", "a -> b")


# --------------------------- dsl ------------------------------------------
def test_build_query_filters_and_time_range():
    spec = QuerySpec.model_validate(
        {
            "text": "timeout",
            "text_fields": ["message"],
            "filters": [
                {"field": "status", "op": "eq", "value": "error"},
                {"field": "code", "op": "gte", "value": 500},
                {"field": "env", "op": "neq", "value": "dev"},
            ],
            "time_field": "@timestamp",
            "time_from": "now-24h",
            "sort": ["@timestamp:desc"],
            "aggs": [{"name": "by_service", "type": "terms", "field": "service.keyword", "size": 5}],
        }
    )
    body = build_query(spec)
    b = body["query"]["bool"]
    assert {"term": {"status": "error"}} in b["filter"]
    assert {"range": {"code": {"gte": 500}}} in b["filter"]
    assert {"term": {"env": "dev"}} in b["must_not"]
    assert {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}} in b["filter"]
    assert b["must"][0]["multi_match"]["fields"] == ["message"]
    assert body["sort"] == [{"@timestamp": {"order": "desc"}}]
    assert body["aggs"]["by_service"]["terms"]["size"] == 5
    assert body["size"] == 0  # agg-only defaults to no hits


def test_build_query_empty_spec_is_match_all():
    assert build_query(QuerySpec())["query"] == {"match_all": {}}


# --------------------------- client ---------------------------------------
async def test_client_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"ok": True})

    c = ESClient(make_settings(max_retries=2), transport=httpx.MockTransport(handler))
    assert await c.get("/_cluster/health") == {"ok": True}
    assert calls["n"] == 2
    await c.aclose()


async def test_client_raises_readable_es_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"type": "search_phase_execution_exception", "reason": "No mapping for [ts]"}},
        )

    c = ESClient(make_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ElasticsearchError) as exc:
        await c.post("/logs/_search", body={})
    assert "No mapping for [ts]" in str(exc.value)
    await c.aclose()


async def test_client_transport_failure_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c = ESClient(make_settings(max_retries=1), transport=httpx.MockTransport(handler))
    with pytest.raises(TransportError):
        await c.get("/")
    await c.aclose()


# --------------------------- tools ----------------------------------------
def build_ctx(handler, **kw) -> ToolContext:
    s = make_settings(**kw)
    return ToolContext(client=ESClient(s, transport=httpx.MockTransport(handler)), guard=Guard(s), settings=s)


async def test_run_query_caps_size_and_summarizes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "took": 12,
                "timed_out": False,
                "_shards": {"total": 2, "successful": 2, "failed": 0},
                "hits": {
                    "total": {"value": 3, "relation": "eq"},
                    "max_score": 1.0,
                    "hits": [{"_index": "logs", "_id": "1", "_score": 1.0, "_source": {"msg": "hi"}}],
                },
            },
        )

    ctx = build_ctx(handler, max_result_size=25)
    server = FakeServer()
    search_tools.register(server, ctx)
    out = json.loads(await server.tools["run_query"](index="logs", query='{"query":{"match_all":{}}}', size=999))
    assert seen["body"]["size"] == 25
    assert out["total_hits"] == 3
    assert out["hits"][0]["_id"] == "1"
    await ctx.client.aclose()


async def test_run_query_returns_error_text_not_exception():
    ctx = build_ctx(lambda r: httpx.Response(200, json={}), index_allow=["logs-*"])
    server = FakeServer()
    search_tools.register(server, ctx)
    out = await server.tools["run_query"](index="secrets")
    assert out.startswith("ERROR (run_query)") and "allow-list" in out
    await ctx.client.aclose()


async def test_generate_dsl_validates_and_lists_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        if "_validate" in request.url.path:
            return httpx.Response(200, json={"valid": True, "explanations": [{"index": "logs"}]})
        if "_mapping" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "logs": {
                        "mappings": {
                            "properties": {
                                "@timestamp": {"type": "date"},
                                "service": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, json={})

    ctx = build_ctx(handler)
    server = FakeServer()
    search_tools.register(server, ctx)
    out = json.loads(
        await server.tools["generate_dsl"](
            index="logs", spec='{"text":"error","time_field":"@timestamp","time_from":"now-1h"}'
        )
    )
    assert out["valid"] is True
    assert out["field_catalog"]["service"].startswith("text")
    assert "@timestamp" in out["field_catalog"]
    await ctx.client.aclose()


# --------------------------- delete_by_query ------------------------------
async def test_delete_by_query_refuses_match_all():
    ctx = build_ctx(lambda r: httpx.Response(200, json={}), read_only=False, allow_destructive=True)
    server = FakeServer()
    ops_tools.register(server, ctx)
    out = await server.tools["delete_by_query"](
        index="logs", query='{"query":{"match_all":{}}}', confirm=True
    )
    assert out.startswith("ERROR (delete_by_query)") and "match_all" in out
    await ctx.client.aclose()


async def test_delete_by_query_blocked_when_not_destructive():
    ctx = build_ctx(lambda r: httpx.Response(200, json={}), read_only=False, allow_destructive=False)
    server = FakeServer()
    ops_tools.register(server, ctx)
    out = await server.tools["delete_by_query"](
        index="logs", query='{"term":{"env":"dev"}}', confirm=True
    )
    assert out.startswith("ERROR (delete_by_query)") and "destructive" in out
    await ctx.client.aclose()


async def test_delete_by_query_previews_and_runs():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_count"):
            return httpx.Response(200, json={"count": 7})
        return httpx.Response(200, json={"task": "node:42"})

    ctx = build_ctx(handler, read_only=False, allow_destructive=True)
    server = FakeServer()
    ops_tools.register(server, ctx)
    out = json.loads(
        await server.tools["delete_by_query"](index="logs", query='{"term":{"env":"dev"}}', confirm=True)
    )
    assert out["matched_before_delete"] == 7
    assert out["result"]["task"] == "node:42"
    await ctx.client.aclose()


# --------------------------- update_settings ------------------------------
async def test_update_settings_rejects_static():
    ctx = build_ctx(lambda r: httpx.Response(200, json={}), read_only=False, allow_destructive=True)
    server = FakeServer()
    ops_tools.register(server, ctx)
    out = await server.tools["update_settings"](
        index="logs", settings='{"number_of_shards":5}', confirm=True
    )
    assert out.startswith("ERROR (update_settings)") and "static" in out
    await ctx.client.aclose()


async def test_update_settings_applies_dynamic():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"acknowledged": True})

    ctx = build_ctx(handler, read_only=False, allow_destructive=True)
    server = FakeServer()
    ops_tools.register(server, ctx)
    out = json.loads(
        await server.tools["update_settings"](
            index="logs", settings='{"number_of_replicas":2}', confirm=True
        )
    )
    assert out["acknowledged"] is True
    assert seen["body"] == {"index": {"number_of_replicas": 2}}
    await ctx.client.aclose()


# --------------------------- PIT paging -----------------------------------
async def test_paged_search_adds_tiebreak_and_pit():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "pit_id": "refreshed-pit",
                "hits": {
                    "total": {"value": 1, "relation": "eq"},
                    "hits": [{"_index": "logs", "_id": "9", "_score": None, "_source": {"m": 1}, "sort": [123, 4]}],
                },
            },
        )

    ctx = build_ctx(handler)
    server = FakeServer()
    search_tools.register(server, ctx)
    out = json.loads(
        await server.tools["paged_search"](pit_id="p1", sort=["@timestamp:desc"])
    )
    assert seen["path"] == "/_search"  # no index in path with a PIT
    assert seen["body"]["pit"]["id"] == "p1"
    assert {"_shard_doc": {"order": "asc"}} in seen["body"]["sort"]
    assert out["pit_id"] == "refreshed-pit"
    assert out["next_search_after"] == [123, 4]
    await ctx.client.aclose()


# --------------------------- SQL ------------------------------------------
async def test_sql_query_rejects_mutation():
    ctx = build_ctx(lambda r: httpx.Response(200, json={}))
    server = FakeServer()
    sql_tools.register(server, ctx)
    out = await server.tools["sql_query"](query="DELETE FROM logs WHERE x=1")
    assert out.startswith("ERROR (sql_query)") and "SELECT" in out
    await ctx.client.aclose()


async def test_sql_query_enforces_index_policy():
    ctx = build_ctx(lambda r: httpx.Response(200, json={}), index_allow=["logs-*"])
    server = FakeServer()
    sql_tools.register(server, ctx)
    out = await server.tools["sql_query"](query='SELECT * FROM "secrets"')
    assert out.startswith("ERROR (sql_query)") and "allow-list" in out
    await ctx.client.aclose()


async def test_sql_query_runs_and_returns_rows():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "columns": [{"name": "status", "type": "keyword"}, {"name": "c", "type": "long"}],
                "rows": [["error", 5], ["ok", 90]],
                "cursor": "abc==",
            },
        )

    ctx = build_ctx(handler, index_allow=["logs-*"], max_result_size=50)
    server = FakeServer()
    sql_tools.register(server, ctx)
    out = json.loads(
        await server.tools["sql_query"](
            query='SELECT status, COUNT(*) c FROM "logs-app" GROUP BY status', fetch_size=999
        )
    )
    assert seen["body"]["fetch_size"] == 50  # clamped by max_result_size
    assert out["rows"][0] == ["error", 5]
    assert out["cursor"] == "abc=="
    await ctx.client.aclose()


# --------------------------- ingest ---------------------------------------
async def test_index_document_needs_confirm():
    ctx = build_ctx(lambda r: httpx.Response(200, json={}), read_only=False)
    server = FakeServer()
    ingest_tools.register(server, ctx)
    out = await server.tools["index_document"](index="logs", document='{"a":1}')
    assert out.startswith("ERROR (index_document)") and "confirm=true" in out
    await ctx.client.aclose()


async def test_bulk_index_builds_ndjson_and_counts():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ctype"] = request.headers.get("content-type")
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "errors": True,
                "items": [
                    {"index": {"status": 201}},
                    {"index": {"status": 400, "error": {"reason": "bad"}}},
                ],
            },
        )

    ctx = build_ctx(handler, read_only=False)
    server = FakeServer()
    ingest_tools.register(server, ctx)
    out = json.loads(
        await server.tools["bulk_index"](
            index="logs", documents='[{"id":"a","v":1},{"id":"b","v":2}]', id_field="id", confirm=True
        )
    )
    assert seen["ctype"] == "application/x-ndjson"
    assert '"_id": "a"' in seen["body"]  # id_field became the doc id
    assert out["total"] == 2 and out["succeeded"] == 1
    assert out["first_errors"][0]["status"] == 400
    await ctx.client.aclose()


# --------------------------- update_by_query & aliases --------------------
async def test_update_by_query_refuses_match_all():
    ctx = build_ctx(lambda r: httpx.Response(200, json={}), read_only=False, allow_destructive=True)
    server = FakeServer()
    ops_tools.register(server, ctx)
    out = await server.tools["update_by_query"](
        index="logs", query='{"query":{"match_all":{}}}', script_source="ctx._source.x=1", confirm=True
    )
    assert out.startswith("ERROR (update_by_query)") and "match_all" in out
    await ctx.client.aclose()


async def test_update_by_query_previews_and_scripts():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_count"):
            return httpx.Response(200, json={"count": 4})
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"task": "n:1"})

    ctx = build_ctx(handler, read_only=False, allow_destructive=True)
    server = FakeServer()
    ops_tools.register(server, ctx)
    out = json.loads(
        await server.tools["update_by_query"](
            index="logs", query='{"term":{"level":"warn"}}',
            script_source="ctx._source.level='error'", confirm=True,
        )
    )
    assert out["matched_before_update"] == 4
    assert seen["body"]["script"]["lang"] == "painless"
    await ctx.client.aclose()


async def test_alias_actions_checks_policy_and_confirm():
    ctx = build_ctx(lambda r: httpx.Response(200, json={"acknowledged": True}),
                    read_only=False, index_allow=["logs-*"])
    server = FakeServer()
    ops_tools.register(server, ctx)
    # target outside allow-list is refused
    out = await server.tools["alias_actions"](
        actions='[{"add":{"index":"secrets","alias":"x"}}]', confirm=True)
    assert out.startswith("ERROR (alias_actions)") and "allow-list" in out
    # valid swap acknowledged
    ok = json.loads(await server.tools["alias_actions"](
        actions='[{"add":{"index":"logs-new","alias":"logs"}},{"remove":{"index":"logs-old","alias":"logs"}}]',
        confirm=True))
    assert ok["acknowledged"] is True
    await ctx.client.aclose()


# --------------------------- field_caps -----------------------------------
async def test_field_caps_flags_type_conflicts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "fields": {
                    "code": {
                        "long": {"searchable": True, "aggregatable": True, "indices": ["a"]},
                        "keyword": {"searchable": True, "aggregatable": True, "indices": ["b"]},
                    },
                    "msg": {"text": {"searchable": True, "aggregatable": False}},
                }
            },
        )

    ctx = build_ctx(handler)
    server = FakeServer()
    health_tools.register(server, ctx)
    out = json.loads(await server.tools["field_caps"](index="logs-*"))
    assert out["fields"]["msg"] == "text"
    assert out["fields"]["code"] == ["long", "keyword"]
    assert "code" in out["type_conflicts"]
    await ctx.client.aclose()
