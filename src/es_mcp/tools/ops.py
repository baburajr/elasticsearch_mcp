from __future__ import annotations

import json
from typing import Any

from ..errors import SafetyError
from ._base import ToolContext, guarded


def register(server: Any, ctx: ToolContext) -> None:
    ro = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
    rw = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}

    # ---------------- snapshots -------------------------------------------
    @server.tool(
        name="list_snapshot_repositories",
        description="List registered snapshot repositories with their type and settings (bucket, base_path, ...).",
        annotations=ro,
    )
    @guarded(ctx, "list_snapshot_repositories")
    async def list_snapshot_repositories() -> str:
        repos = await ctx.client.get("/_snapshot/_all")
        return ctx.render(repos)

    @server.tool(
        name="list_snapshots",
        description=(
            "List snapshots in a repository with state, start/end time, duration, indices count, shard "
            "failures and size. Use this before restoring to pick the right snapshot."
        ),
        annotations=ro,
    )
    @guarded(ctx, "list_snapshots")
    async def list_snapshots(repository: str, snapshot: str = "*", verbose: bool = False) -> str:
        resp = await ctx.client.get(
            f"/_snapshot/{repository}/{snapshot}",
            params={"ignore_unavailable": True, "verbose": verbose},
        )
        snaps = [
            {
                "snapshot": s.get("snapshot"),
                "state": s.get("state"),
                "start": s.get("start_time"),
                "duration_ms": s.get("duration_in_millis"),
                "indices_count": len(s.get("indices", [])),
                "indices": s.get("indices", [])[:20] if verbose else None,
                "shards": s.get("shards"),
                "failures": s.get("failures", [])[:3],
            }
            for s in resp.get("snapshots", [])
        ]
        return ctx.render({"count": len(snaps), "snapshots": snaps[-50:]})

    @server.tool(
        name="snapshot_status",
        description="Detailed progress of running or recent snapshots: per-shard stage, bytes done vs total.",
        annotations=ro,
    )
    @guarded(ctx, "snapshot_status")
    async def snapshot_status(repository: str | None = None, snapshot: str | None = None) -> str:
        path = "/_snapshot/_status"
        if repository and snapshot:
            path = f"/_snapshot/{repository}/{snapshot}/_status"
        elif repository:
            path = f"/_snapshot/{repository}/_current/_status"
        resp = await ctx.client.get(path)
        return ctx.render(
            {
                "snapshots": [
                    {
                        "snapshot": s.get("snapshot"),
                        "state": s.get("state"),
                        "shards_stats": s.get("shards_stats"),
                        "stats": s.get("stats"),
                    }
                    for s in resp.get("snapshots", [])
                ]
            }
        )

    @server.tool(
        name="create_snapshot",
        description=(
            "Create a snapshot of selected indices into a repository. Non-blocking by default: returns "
            "immediately, poll with snapshot_status. Requires writes enabled and confirm=true."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )
    @guarded(ctx, "create_snapshot")
    async def create_snapshot(
        repository: str,
        snapshot: str,
        indices: str = "*",
        include_global_state: bool = False,
        wait_for_completion: bool = False,
        confirm: bool = False,
    ) -> str:
        ctx.guard.check_write("create_snapshot")
        idx = ctx.guard.check_index(indices)
        ctx.guard.check_confirm(confirm, "create_snapshot", f"{repository}/{snapshot}")
        body = {
            "indices": idx,
            "include_global_state": include_global_state,
            "ignore_unavailable": True,
        }
        resp = await ctx.client.put(
            f"/_snapshot/{repository}/{snapshot}",
            params={"wait_for_completion": wait_for_completion},
            body=body,
        )
        return ctx.render(resp)

    @server.tool(
        name="restore_snapshot",
        description=(
            "Restore indices from a snapshot. DESTRUCTIVE: an index that already exists must be closed or "
            "renamed via rename_pattern/rename_replacement, otherwise the restore fails. Requires writes "
            "enabled, ES_MCP_ALLOW_DESTRUCTIVE=true, and confirm=true."
        ),
        annotations=rw,
    )
    @guarded(ctx, "restore_snapshot")
    async def restore_snapshot(
        repository: str,
        snapshot: str,
        indices: str,
        rename_pattern: str | None = None,
        rename_replacement: str | None = None,
        include_aliases: bool = False,
        wait_for_completion: bool = False,
        confirm: bool = False,
    ) -> str:
        ctx.guard.check_write("restore_snapshot")
        idx = ctx.guard.check_index(indices, write=True)
        ctx.guard.check_confirm(confirm, "restore_snapshot", f"{repository}/{snapshot} -> {idx}")

        existing = await ctx.client.get(
            f"/_cat/indices/{idx}", params={"format": "json", "h": "index", "expand_wildcards": "open"}
        )
        open_conflicts = [r.get("index") for r in (existing or []) if isinstance(r, dict)]
        if open_conflicts and not rename_pattern:
            raise SafetyError(
                f"these indices already exist and are open: {open_conflicts[:10]}. "
                "Restore would fail. Provide rename_pattern/rename_replacement, or close them first."
            )

        body: dict[str, Any] = {
            "indices": idx,
            "include_aliases": include_aliases,
            "include_global_state": False,
        }
        if rename_pattern:
            body["rename_pattern"] = rename_pattern
            body["rename_replacement"] = rename_replacement or "restored_$1"
        resp = await ctx.client.post(
            f"/_snapshot/{repository}/{snapshot}/_restore",
            params={"wait_for_completion": wait_for_completion},
            body=body,
        )
        return ctx.render({"restore": resp, "poll_with": "index_health or cluster_health"})

    # ---------------- reindex & tasks --------------------------------------
    @server.tool(
        name="reindex",
        description=(
            "Copy documents from a source index to a destination, optionally filtered by a query and "
            "transformed by a pipeline or script. Runs asynchronously (wait_for_completion=false) and returns "
            "a task_id to poll with get_task. Use this to change a field type, reshard, or migrate data. "
            "Requires writes enabled, ES_MCP_ALLOW_DESTRUCTIVE=true, and confirm=true."
        ),
        annotations=rw,
    )
    @guarded(ctx, "reindex")
    async def reindex(
        source_index: str,
        dest_index: str,
        query: str | dict | None = None,
        pipeline: str | None = None,
        script_source: str | None = None,
        op_type: str = "index",
        slices: str | int = "auto",
        requests_per_second: int = -1,
        wait_for_completion: bool = False,
        confirm: bool = False,
    ) -> str:
        ctx.guard.check_write("reindex")
        src = ctx.guard.check_index(source_index)
        dst = ctx.guard.check_index(dest_index, write=True)
        if src == dst:
            raise SafetyError("source and destination must differ")
        ctx.guard.check_confirm(confirm, "reindex", f"{src} -> {dst}")

        source: dict[str, Any] = {"index": src}
        if query:
            q = query if isinstance(query, dict) else json.loads(query)
            source["query"] = q.get("query", q)
        dest: dict[str, Any] = {"index": dst, "op_type": op_type}
        if pipeline:
            dest["pipeline"] = pipeline
        body: dict[str, Any] = {"source": source, "dest": dest, "conflicts": "proceed"}
        if script_source:
            body["script"] = {"lang": "painless", "source": script_source}

        preview = await ctx.client.post(
            f"/{src}/_count", body={"query": source.get("query", {"match_all": {}})}
        )
        resp = await ctx.client.post(
            "/_reindex",
            params={
                "wait_for_completion": wait_for_completion,
                "slices": slices,
                "requests_per_second": requests_per_second,
                "refresh": True,
            },
            body=body,
        )
        return ctx.render(
            {
                "source_doc_count": preview.get("count"),
                "result": resp,
                "poll_with": "get_task(task_id) if a task was returned",
            }
        )

    @server.tool(
        name="get_task",
        description="Poll a long-running task (reindex, update_by_query, delete_by_query) for progress and errors.",
        annotations=ro,
    )
    @guarded(ctx, "get_task")
    async def get_task(task_id: str, wait_for_completion: bool = False) -> str:
        resp = await ctx.client.get(
            f"/_tasks/{task_id}", params={"wait_for_completion": wait_for_completion, "timeout": "30s"}
        )
        task = resp.get("task", resp)
        status = task.get("status", {}) if isinstance(task, dict) else {}
        return ctx.render(
            {
                "completed": resp.get("completed"),
                "action": task.get("action"),
                "running_time_ms": round(task.get("running_time_in_nanos", 0) / 1e6, 1),
                "status": {
                    k: status.get(k)
                    for k in ("total", "created", "updated", "deleted", "batches", "version_conflicts", "requests_per_second", "throttled_millis")
                },
                "error": resp.get("error"),
                "failures": (resp.get("response") or {}).get("failures", [])[:3],
            }
        )

    @server.tool(
        name="cancel_task",
        description="Cancel a running task by id. Use for a runaway reindex or a search eating the cluster.",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    @guarded(ctx, "cancel_task")
    async def cancel_task(task_id: str, confirm: bool = False) -> str:
        ctx.guard.check_write("cancel_task")
        ctx.guard.check_confirm(confirm, "cancel_task", task_id)
        resp = await ctx.client.post(f"/_tasks/{task_id}/_cancel")
        return ctx.render(resp)

    # ---------------- delete & settings -----------------------------------
    @server.tool(
        name="delete_by_query",
        description=(
            "Delete documents matching a query from an index. DESTRUCTIVE and irreversible. A query is "
            "mandatory: match_all is refused so you cannot wipe an index by accident. Runs asynchronously "
            "(wait_for_completion=false) and returns a task_id to poll with get_task. Reports how many "
            "documents match before deleting. Requires writes enabled, ES_MCP_ALLOW_DESTRUCTIVE=true, and "
            "confirm=true."
        ),
        annotations=rw,
    )
    @guarded(ctx, "delete_by_query")
    async def delete_by_query(
        index: str,
        query: str | dict,
        slices: str | int = "auto",
        requests_per_second: int = -1,
        wait_for_completion: bool = False,
        confirm: bool = False,
    ) -> str:
        ctx.guard.check_write("delete_by_query")
        idx = ctx.guard.check_index(index, write=True)
        q = query if isinstance(query, dict) else json.loads(query)
        inner = q.get("query", q)
        if not inner or inner == {"match_all": {}}:
            raise SafetyError(
                "delete_by_query refuses match_all / empty query. Provide a selective query; "
                "to empty an index intentionally, delete and recreate it outside this server."
            )
        ctx.guard.check_confirm(confirm, "delete_by_query", idx)

        preview = await ctx.client.post(f"/{idx}/_count", body={"query": inner})
        resp = await ctx.client.post(
            f"/{idx}/_delete_by_query",
            params={
                "wait_for_completion": wait_for_completion,
                "slices": slices,
                "requests_per_second": requests_per_second,
                "conflicts": "proceed",
                "refresh": True,
            },
            body={"query": inner},
        )
        return ctx.render(
            {
                "matched_before_delete": preview.get("count"),
                "result": resp,
                "poll_with": "get_task(task_id) if a task was returned",
            }
        )

    @server.tool(
        name="update_settings",
        description=(
            "Update dynamic index settings such as number_of_replicas, refresh_interval, "
            "max_result_window, or blocks.*. Static settings (e.g. number_of_shards) cannot be changed on a "
            "live index and are refused. Requires writes enabled, ES_MCP_ALLOW_DESTRUCTIVE=true, and "
            "confirm=true."
        ),
        annotations=rw,
    )
    @guarded(ctx, "update_settings")
    async def update_settings(index: str, settings: str | dict, confirm: bool = False) -> str:
        ctx.guard.check_write("update_settings")
        idx = ctx.guard.check_index(index, write=True)
        body = settings if isinstance(settings, dict) else json.loads(settings)
        if not isinstance(body, dict) or not body:
            raise SafetyError("settings must be a non-empty JSON object")
        # accept both {"index": {...}} and a flat {"number_of_replicas": 1}
        flat = body.get("index", body)
        static = {"number_of_shards", "codec", "routing_partition_size"}
        offenders = [k for k in flat if k.split(".")[-1] in static or k in static]
        if offenders:
            raise SafetyError(f"these settings are static and cannot be updated on a live index: {offenders}")
        payload = body if "index" in body else {"index": body}
        ctx.guard.check_confirm(confirm, "update_settings", idx)
        resp = await ctx.client.put(f"/{idx}/_settings", body=payload)
        return ctx.render({"acknowledged": resp.get("acknowledged"), "index": idx, "applied": payload["index"]})

    @server.tool(
        name="update_by_query",
        description=(
            "Update documents in place by query, using a painless script. DESTRUCTIVE and irreversible. A "
            "query is mandatory: match_all is refused so you cannot rewrite a whole index by accident. Runs "
            "asynchronously; returns a task_id to poll with get_task. Reports how many documents match before "
            "updating. Requires writes enabled, ES_MCP_ALLOW_DESTRUCTIVE=true, and confirm=true."
        ),
        annotations=rw,
    )
    @guarded(ctx, "update_by_query")
    async def update_by_query(
        index: str,
        query: str | dict,
        script_source: str,
        script_params: str | dict | None = None,
        slices: str | int = "auto",
        requests_per_second: int = -1,
        wait_for_completion: bool = False,
        confirm: bool = False,
    ) -> str:
        ctx.guard.check_write("update_by_query")
        idx = ctx.guard.check_index(index, write=True)
        q = query if isinstance(query, dict) else json.loads(query)
        inner = q.get("query", q)
        if not inner or inner == {"match_all": {}}:
            raise SafetyError("update_by_query refuses match_all / empty query; provide a selective query")
        if not script_source:
            raise SafetyError("script_source is required")
        ctx.guard.check_confirm(confirm, "update_by_query", idx)

        script: dict[str, Any] = {"lang": "painless", "source": script_source}
        if script_params:
            script["params"] = script_params if isinstance(script_params, dict) else json.loads(script_params)
        preview = await ctx.client.post(f"/{idx}/_count", body={"query": inner})
        resp = await ctx.client.post(
            f"/{idx}/_update_by_query",
            params={
                "wait_for_completion": wait_for_completion,
                "slices": slices,
                "requests_per_second": requests_per_second,
                "conflicts": "proceed",
                "refresh": True,
            },
            body={"query": inner, "script": script},
        )
        return ctx.render(
            {"matched_before_update": preview.get("count"), "result": resp, "poll_with": "get_task(task_id)"}
        )

    @server.tool(
        name="alias_actions",
        description=(
            "Add or remove index aliases atomically in one request. Pass actions as a JSON array, e.g. "
            '[{"add": {"index": "logs-2026", "alias": "logs"}}, {"remove": {"index": "logs-2025", "alias": '
            '"logs"}}]. Use this to swap an alias from an old index to a new one with zero downtime after a '
            "reindex. Only add/remove are allowed. Every index touched is checked against the write policy. "
            "Requires writes enabled and confirm=true."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )
    @guarded(ctx, "alias_actions")
    async def alias_actions(actions: str | list, confirm: bool = False) -> str:
        ctx.guard.check_write("alias_actions")
        acts = actions if isinstance(actions, list) else json.loads(actions)
        if not isinstance(acts, list) or not acts:
            raise SafetyError("actions must be a non-empty JSON array")
        for a in acts:
            if not isinstance(a, dict) or not (set(a) <= {"add", "remove"}):
                raise SafetyError("each action must be a single {'add': {...}} or {'remove': {...}}")
            for spec in a.values():
                target = spec.get("index") or spec.get("indices")
                if target:
                    ctx.guard.check_index(target, write=True)
        ctx.guard.check_confirm(confirm, "alias_actions", ",".join(sorted({next(iter(a)) for a in acts})))
        resp = await ctx.client.post("/_aliases", body={"actions": acts})
        return ctx.render({"acknowledged": resp.get("acknowledged"), "actions": acts})
