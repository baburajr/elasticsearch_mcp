from __future__ import annotations

from typing import Any

from ..formatting import parse_cat
from ._base import ToolContext, guarded


def _ms(v: Any) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def register(server: Any, ctx: ToolContext) -> None:
    ro = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}

    @server.tool(
        name="cluster_health",
        description=(
            "Cluster health overview: status, node counts, active/relocating/initializing/unassigned shards, "
            "pending tasks, and (optionally) per-index health. Start troubleshooting here."
        ),
        annotations=ro,
    )
    @guarded(ctx, "cluster_health")
    async def cluster_health(level: str = "cluster", include_nodes: bool = False) -> str:
        health = await ctx.client.get("/_cluster/health", params={"level": level})
        out: dict[str, Any] = {"health": health}
        pending = await ctx.client.get("/_cluster/pending_tasks")
        out["pending_tasks"] = (pending or {}).get("tasks", [])[:10]

        if include_nodes:
            stats = await ctx.client.get("/_nodes/stats/jvm,os,fs,thread_pool")
            nodes = []
            for nid, n in (stats.get("nodes") or {}).items():
                jvm = n.get("jvm", {}).get("mem", {})
                fs = n.get("fs", {}).get("total", {})
                tp = n.get("thread_pool", {})
                nodes.append(
                    {
                        "node": n.get("name", nid),
                        "roles": n.get("roles"),
                        "heap_used_percent": jvm.get("heap_used_percent"),
                        "disk_free_bytes": fs.get("available_in_bytes"),
                        "cpu_percent": n.get("os", {}).get("cpu", {}).get("percent"),
                        "search_queue": tp.get("search", {}).get("queue"),
                        "search_rejected": tp.get("search", {}).get("rejected"),
                        "write_rejected": tp.get("write", {}).get("rejected"),
                    }
                )
            out["nodes"] = nodes
        return ctx.render(out)

    @server.tool(
        name="index_health",
        description=(
            "Per-index health and size: status, docs, store size, primaries/replicas, segment count, refresh "
            "and merge stats, plus search/index throughput. Pass an index pattern to narrow it down."
        ),
        annotations=ro,
    )
    @guarded(ctx, "index_health")
    async def index_health(index: str = "*", include_settings: bool = False) -> str:
        idx = ctx.guard.check_index(index)
        cat = await ctx.client.get(
            f"/_cat/indices/{idx}",
            params={
                "format": "json",
                "bytes": "b",
                "h": "health,status,index,pri,rep,docs.count,docs.deleted,store.size,pri.store.size,creation.date.string",
                "s": "store.size:desc",
                "expand_wildcards": "open",
            },
        )
        stats = await ctx.client.get(
            f"/{idx}/_stats/docs,store,indexing,search,refresh,merge,segments",
            params={"ignore_unavailable": True},
        )
        per_index = {}
        for name, s in (stats.get("indices") or {}).items():
            total = s.get("total", {})
            search = total.get("search", {})
            indexing = total.get("indexing", {})
            per_index[name] = {
                "segments": total.get("segments", {}).get("count"),
                "search_query_total": search.get("query_total"),
                "avg_query_ms": round(
                    search.get("query_time_in_millis", 0) / max(search.get("query_total", 0), 1), 2
                ),
                "search_fetch_avg_ms": round(
                    search.get("fetch_time_in_millis", 0) / max(search.get("fetch_total", 0), 1), 2
                ),
                "current_searches": search.get("query_current"),
                "index_total": indexing.get("index_total"),
                "avg_index_ms": round(
                    indexing.get("index_time_in_millis", 0) / max(indexing.get("index_total", 0), 1), 2
                ),
                "merges_current": total.get("merges", {}).get("current"),
                "refresh_total": total.get("refresh", {}).get("total"),
            }
        out: dict[str, Any] = {"indices": parse_cat(cat), "stats": per_index}

        if include_settings:
            settings = await ctx.client.get(
                f"/{idx}/_settings", params={"flat_settings": True, "ignore_unavailable": True}
            )
            out["settings"] = {
                name: {
                    k: v
                    for k, v in (s.get("settings") or {}).items()
                    if any(
                        key in k
                        for key in (
                            "number_of_shards",
                            "number_of_replicas",
                            "refresh_interval",
                            "max_result_window",
                            "slowlog",
                            "lifecycle",
                            "blocks",
                        )
                    )
                }
                for name, s in settings.items()
            }
        return ctx.render(out)

    @server.tool(
        name="shard_allocation",
        description=(
            "Shard placement and why shards are unassigned. Returns _cat/shards, per-node disk usage, and for "
            "any UNASSIGNED shard the cluster allocation explanation (decider-level reasons: disk watermark, "
            "awareness, filtering, max_retries). This is the tool for a yellow/red cluster."
        ),
        annotations=ro,
    )
    @guarded(ctx, "shard_allocation")
    async def shard_allocation(index: str = "*", only_problems: bool = True) -> str:
        idx = ctx.guard.check_index(index)
        shards = parse_cat(
            await ctx.client.get(
                f"/_cat/shards/{idx}",
                params={
                    "format": "json",
                    "bytes": "b",
                    "h": "index,shard,prirep,state,docs,store,node,unassigned.reason,unassigned.details",
                },
            )
        )
        problems = [s for s in shards if s.get("state") not in ("STARTED", "RELOCATING")]
        out: dict[str, Any] = {
            "total_shards": len(shards),
            "unhealthy_shards": len(problems),
            "shards": problems if only_problems else shards[:200],
            "disk": parse_cat(
                await ctx.client.get(
                    "/_cat/allocation",
                    params={"format": "json", "bytes": "b", "h": "node,shards,disk.used,disk.avail,disk.percent"},
                )
            ),
        }

        explanations = []
        for s in problems[:3]:
            body = {
                "index": s.get("index"),
                "shard": int(s.get("shard", 0)),
                "primary": s.get("prirep") == "p",
            }
            try:
                exp = await ctx.client.post("/_cluster/allocation/explain", body=body)
                explanations.append(
                    {
                        "index": exp.get("index"),
                        "shard": exp.get("shard"),
                        "primary": exp.get("primary"),
                        "current_state": exp.get("current_state"),
                        "unassigned_info": exp.get("unassigned_info"),
                        "can_allocate": exp.get("can_allocate"),
                        "allocate_explanation": exp.get("allocate_explanation"),
                        "node_decisions": [
                            {
                                "node": d.get("node_name"),
                                "decision": d.get("node_decision"),
                                "deciders": [
                                    {"decider": x.get("decider"), "explanation": x.get("explanation")}
                                    for x in (d.get("deciders") or [])
                                    if x.get("decision") in ("NO", "THROTTLE")
                                ],
                            }
                            for d in (exp.get("node_allocation_decisions") or [])[:5]
                        ],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                explanations.append({"index": s.get("index"), "error": str(exc)})
        if explanations:
            out["allocation_explain"] = explanations
        return ctx.render(out)

    @server.tool(
        name="find_slow_queries",
        description=(
            "Find what is slow. Combines: indices ranked by average query latency (_stats), currently running "
            "search tasks with elapsed time (_tasks), search thread-pool queue/rejection counts per node, and "
            "the configured search slowlog thresholds per index (so you know whether slowlog is even on). "
            "Use top_n to control how many indices come back."
        ),
        annotations=ro,
    )
    @guarded(ctx, "find_slow_queries")
    async def find_slow_queries(index: str = "*", top_n: int = 10, min_avg_ms: float = 0.0) -> str:
        idx = ctx.guard.check_index(index)
        stats = await ctx.client.get(f"/{idx}/_stats/search", params={"ignore_unavailable": True})
        ranked = []
        for name, s in (stats.get("indices") or {}).items():
            search = s.get("total", {}).get("search", {})
            qt, qtot = search.get("query_time_in_millis", 0), search.get("query_total", 0)
            if not qtot:
                continue
            avg = qt / qtot
            if avg < min_avg_ms:
                continue
            ranked.append(
                {
                    "index": name,
                    "avg_query_ms": _ms(avg),
                    "query_total": qtot,
                    "total_query_time_ms": qt,
                    "avg_fetch_ms": _ms(
                        search.get("fetch_time_in_millis", 0) / max(search.get("fetch_total", 0), 1)
                    ),
                    "scroll_current": search.get("scroll_current"),
                    "suggest_total": search.get("suggest_total"),
                }
            )
        ranked.sort(key=lambda r: r["avg_query_ms"], reverse=True)

        tasks_resp = await ctx.client.get(
            "/_tasks", params={"actions": "*search*", "detailed": True, "group_by": "none"}
        )
        running = [
            {
                "task_id": f"{t.get('node')}:{t.get('id')}",
                "action": t.get("action"),
                "running_time_ms": round(t.get("running_time_in_nanos", 0) / 1e6, 1),
                "description": (t.get("description") or "")[:400],
                "cancellable": t.get("cancellable"),
            }
            for t in (tasks_resp.get("tasks") or [])
        ]
        running.sort(key=lambda t: t["running_time_ms"], reverse=True)

        node_stats = await ctx.client.get("/_nodes/stats/thread_pool")
        pools = [
            {
                "node": n.get("name", nid),
                "search_queue": n.get("thread_pool", {}).get("search", {}).get("queue"),
                "search_active": n.get("thread_pool", {}).get("search", {}).get("active"),
                "search_rejected": n.get("thread_pool", {}).get("search", {}).get("rejected"),
            }
            for nid, n in (node_stats.get("nodes") or {}).items()
        ]

        slowlog = {}
        try:
            settings = await ctx.client.get(
                f"/{idx}/_settings", params={"flat_settings": True, "ignore_unavailable": True}
            )
            for name, s in list(settings.items())[:top_n]:
                thresholds = {
                    k.replace("index.search.slowlog.threshold.", ""): v
                    for k, v in (s.get("settings") or {}).items()
                    if "search.slowlog" in k
                }
                if thresholds:
                    slowlog[name] = thresholds
        except Exception:  # noqa: BLE001
            pass

        return ctx.render(
            {
                "slowest_indices": ranked[:top_n],
                "running_search_tasks": running[:10],
                "search_thread_pools": pools,
                "slowlog_thresholds": slowlog,
                "hint": (
                    "No slowlog thresholds means slow queries are never logged. Enable with "
                    "index.search.slowlog.threshold.query.warn. Use explain_query(profile=true) on a "
                    "suspect query to find the slow clause."
                ),
            }
        )

    @server.tool(
        name="cat_nodes",
        description=(
            "List cluster nodes with role, version, heap/RAM/CPU/load, and master flag. Use it to spot a hot "
            "node, a version mismatch across nodes, or which node is master."
        ),
        annotations=ro,
    )
    @guarded(ctx, "cat_nodes")
    async def cat_nodes() -> str:
        nodes = parse_cat(
            await ctx.client.get(
                "/_cat/nodes",
                params={
                    "format": "json",
                    "h": "name,ip,node.role,master,version,heap.percent,ram.percent,cpu,load_1m,disk.used_percent",
                    "s": "name",
                },
            )
        )
        return ctx.render({"count": len(nodes), "nodes": nodes})

    @server.tool(
        name="field_caps",
        description=(
            "Show the capabilities of one or more fields across indices via _field_caps: the type(s) each "
            "field has, whether it is searchable and aggregatable. The key use is catching a field mapped as "
            "different types in different indices (e.g. long in one, keyword in another), which breaks queries "
            "across an index pattern. Pass fields as a comma list or *."
        ),
        annotations=ro,
    )
    @guarded(ctx, "field_caps")
    async def field_caps(index: str, fields: str = "*") -> str:
        idx = ctx.guard.check_index(index)
        resp = await ctx.client.get(
            f"/{idx}/_field_caps",
            params={"fields": fields, "ignore_unavailable": True},
        )
        conflicts = {}
        summary = {}
        for field, types in (resp.get("fields") or {}).items():
            type_names = list(types.keys())
            summary[field] = type_names[0] if len(type_names) == 1 else type_names
            if len(type_names) > 1:
                conflicts[field] = {
                    t: {
                        "searchable": c.get("searchable"),
                        "aggregatable": c.get("aggregatable"),
                        "indices": c.get("indices"),
                    }
                    for t, c in types.items()
                }
        out: dict[str, Any] = {"fields": dict(sorted(summary.items())[:300])}
        if conflicts:
            out["type_conflicts"] = conflicts
            out["hint"] = "conflicting field types break queries across the pattern; align mappings or query per-index"
        return ctx.render(out)
