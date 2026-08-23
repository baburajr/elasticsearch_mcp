"""Terminal demo of elasticsearch-mcp against a live cluster.

Runs the real MCP tools (same register path the server uses) and prints paced,
colored output for screen/asciinema recording. Read the story top to bottom.

    python demo/demo.py

Uses demo-* indices only (see ES_MCP_INDEX_ALLOW in .env).
"""

import asyncio
import json
import random
import sys
import time

from es_mcp.config import Settings
from es_mcp.client import ESClient
from es_mcp.safety import Guard
from es_mcp.tools._base import ToolContext
from es_mcp.tools import search, health, mapping, ops, sql, ingest

C = {
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "grey": "\033[90m", "bold": "\033[1m", "off": "\033[0m",
}
IDX = "demo-logs"
PACE = float(sys.argv[1]) if len(sys.argv) > 1 else 1.1  # seconds between steps


def slp(x=None):
    time.sleep(x if x is not None else PACE)


def prompt(cmd):
    print(f"\n{C['green']}${C['off']} {C['bold']}{cmd}{C['off']}")
    slp(0.6)


def note(text):
    print(f"{C['grey']}# {text}{C['off']}")
    slp(0.5)


def out(obj, keep=None):
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:
            print(obj)
            return
    if keep:
        obj = {k: obj.get(k) for k in keep if k in obj}
    print(json.dumps(obj, indent=2)[:1400])


class Reg:
    def __init__(self):
        self.t = {}

    def tool(self, name=None, **_kw):
        def d(fn):
            self.t[name] = fn
            return fn
        return d


LEVELS = ["INFO", "INFO", "INFO", "WARN", "ERROR", "ERROR"]
SERVICES = ["checkout", "auth", "search", "payments", "cart"]
MSGS = {
    "INFO": ["request handled", "cache hit", "user login ok"],
    "WARN": ["slow response", "retry scheduled", "cache miss"],
    "ERROR": ["timeout talking to db", "5xx from upstream", "connection refused"],
}


async def seed(ctx, T):
    await ctx.client.delete(f"/{IDX}", params={"ignore_unavailable": True})
    # create the index with an explicit mapping (put_mapping is additive-only and
    # needs an existing index; here we want keyword fields from the start)
    await ctx.client.put(f"/{IDX}", body={"mappings": {"properties": {
        "@timestamp": {"type": "date"},
        "level": {"type": "keyword"},
        "service": {"type": "keyword"},
        "message": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "latency_ms": {"type": "integer"},
        "status": {"type": "integer"},
    }}})
    docs = []
    for _ in range(500):
        lvl = random.choice(LEVELS)
        docs.append({
            "@timestamp": f"2026-08-23T{random.randint(0,23):02d}:{random.randint(0,59):02d}:00Z",
            "level": lvl,
            "service": random.choice(SERVICES),
            "message": random.choice(MSGS[lvl]),
            "latency_ms": random.randint(5, 900),
            "status": 500 if lvl == "ERROR" else (429 if lvl == "WARN" else 200),
        })
    await T["bulk_index"](index=IDX, documents=json.dumps(docs), refresh=True, confirm=True)
    await ctx.client.post(f"/{IDX}/_refresh")


async def main():
    s = Settings()
    ctx = ToolContext(client=ESClient(s), guard=Guard(s), settings=s)
    r = Reg()
    for m in (search, health, mapping, ops, sql, ingest):
        m.register(r, ctx)
    T = r.t

    print(f"{C['cyan']}{C['bold']}  elasticsearch-mcp — live demo{C['off']}")
    print(f"{C['grey']}  any MCP agent talks to Elasticsearch over REST{C['off']}")
    slp(1.2)

    prompt("# any MCP agent can now call these tools. let's watch.")
    note("seeding a demo-logs index with 500 log lines...")
    await seed(ctx, T)
    print(f"{C['green']}  seeded 500 docs{C['off']}")
    slp()

    prompt("cluster_info")
    out(await ctx.client.ping())
    slp()

    prompt("list_indices(pattern='demo-*')")
    out(await T["list_indices"](pattern="demo-*"), keep=["count", "indices"])
    slp()

    prompt("get_mapping(index='demo-logs')   # never guess field names")
    m = json.loads(await T["get_mapping"](index=IDX))
    out(m[IDX]["fields"])
    slp()

    prompt("run_query   # all ERROR logs, newest first")
    q = await T["run_query"](
        index=IDX,
        query=json.dumps({"query": {"term": {"level": "ERROR"}}, "sort": [{"@timestamp": "desc"}]}),
        size=2,
    )
    out(q, keep=["total_hits", "took_ms", "hits"])
    slp()

    prompt('sql_query   # SELECT service, COUNT(*) ... GROUP BY service')
    sqr = await T["sql_query"](
        query=f'SELECT service, COUNT(*) errors FROM "{IDX}" WHERE level=\'ERROR\' GROUP BY service ORDER BY errors DESC')
    out(sqr, keep=["columns", "rows"])
    slp()

    prompt("generate_dsl   # structured spec -> validated Query DSL")
    g = await T["generate_dsl"](index=IDX, spec=json.dumps({
        "filters": [{"field": "service", "op": "eq", "value": "payments"}],
        "aggs": [{"name": "avg_latency", "type": "avg", "field": "latency_ms"}],
    }))
    out(g, keep=["valid", "dsl"])
    slp()

    print(f"\n{C['yellow']}{C['bold']}  --- safety layer ---{C['off']}")
    slp(0.8)

    prompt("delete_by_query(match_all)   # try to wipe the index")
    res = await T["delete_by_query"](index=IDX, query=json.dumps({"query": {"match_all": {}}}), confirm=True)
    print(f"{C['red']}{res}{C['off']}")
    note("refused. match_all delete is blocked by policy.")
    slp()

    prompt("run_query(index='secret-index')   # outside the allow-list")
    res = await T["run_query"](index="secret-index")
    print(f"{C['red']}{res}{C['off']}")
    note("refused. only demo-* is allowed. writes off by default too.")
    slp()

    prompt("update_by_query   # selective, allowed write")
    u = await T["update_by_query"](
        index=IDX, query=json.dumps({"term": {"level": "WARN"}}),
        script_source="ctx._source.level='REVIEWED'", wait_for_completion=True, confirm=True)
    out(u, keep=["matched_before_update", "result"])
    slp()

    print(f"\n{C['green']}{C['bold']}  30+ tools. safe by default. dangerous only when you say so.{C['off']}")
    print(f"{C['grey']}  github.com/baburajr/elasticsearch_mcp{C['off']}")
    slp(1.5)

    await ctx.client.delete(f"/{IDX}", params={"ignore_unavailable": True})
    await ctx.client.aclose()


asyncio.run(main())
