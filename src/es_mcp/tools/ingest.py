from __future__ import annotations

import json
from typing import Any

from ..errors import SafetyError
from ._base import ToolContext, guarded


def register(server: Any, ctx: ToolContext) -> None:
    rw_ni = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}

    @server.tool(
        name="index_document",
        description=(
            "Index (create or overwrite) a single document into an index. Pass doc as a JSON object. "
            "Optional doc_id: given, it overwrites that id; omitted, ES assigns one. Set refresh=true to make "
            "it searchable immediately (slower). Requires writes enabled and confirm=true."
        ),
        annotations=rw_ni,
    )
    @guarded(ctx, "index_document")
    async def index_document(
        index: str,
        document: str | dict,
        doc_id: str | None = None,
        refresh: bool = False,
        confirm: bool = False,
    ) -> str:
        ctx.guard.check_write("index_document")
        idx = ctx.guard.check_index(index, write=True)
        ctx.guard.check_confirm(confirm, "index_document", idx)
        doc = document if isinstance(document, dict) else json.loads(document)
        if not isinstance(doc, dict) or not doc:
            raise SafetyError("document must be a non-empty JSON object")
        path = f"/{idx}/_doc/{doc_id}" if doc_id else f"/{idx}/_doc"
        method = ctx.client.put if doc_id else ctx.client.post
        resp = await method(path, body=doc, params={"refresh": "true" if refresh else None})
        return ctx.render(
            {"_id": resp.get("_id"), "result": resp.get("result"), "index": idx, "version": resp.get("_version")}
        )

    @server.tool(
        name="bulk_index",
        description=(
            "Bulk index many documents into one index in a single request. Pass documents as a JSON array of "
            "objects; each becomes an index action. Optional id_field names a field to use as the document id. "
            "Reports how many succeeded and the first few errors. Much faster than index_document in a loop. "
            "Requires writes enabled and confirm=true."
        ),
        annotations=rw_ni,
    )
    @guarded(ctx, "bulk_index")
    async def bulk_index(
        index: str,
        documents: str | list,
        id_field: str | None = None,
        refresh: bool = False,
        confirm: bool = False,
    ) -> str:
        ctx.guard.check_write("bulk_index")
        idx = ctx.guard.check_index(index, write=True)
        ctx.guard.check_confirm(confirm, "bulk_index", idx)
        docs = documents if isinstance(documents, list) else json.loads(documents)
        if not isinstance(docs, list) or not docs:
            raise SafetyError("documents must be a non-empty JSON array of objects")
        if len(docs) > 10_000:
            raise SafetyError("bulk_index capped at 10000 documents per call; split into batches")

        lines: list[str] = []
        for d in docs:
            if not isinstance(d, dict):
                raise SafetyError("every document must be a JSON object")
            action: dict[str, Any] = {"index": {"_index": idx}}
            if id_field and d.get(id_field) is not None:
                action["index"]["_id"] = str(d[id_field])
            lines.append(json.dumps(action))
            lines.append(json.dumps(d, default=str))
        ndjson = "\n".join(lines) + "\n"

        resp = await ctx.client.request(
            "POST",
            "/_bulk",
            params={"refresh": "true" if refresh else None},
            body=None,
            content=ndjson,
        )
        items = resp.get("items", []) if isinstance(resp, dict) else []
        errors = [
            {"status": (it.get("index") or {}).get("status"), "error": (it.get("index") or {}).get("error")}
            for it in items
            if (it.get("index") or {}).get("error")
        ]
        return ctx.render(
            {
                "index": idx,
                "total": len(docs),
                "errored": resp.get("errors"),
                "succeeded": len(items) - len(errors),
                "first_errors": errors[:5],
            }
        )
