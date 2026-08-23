"""Shared plumbing for tool modules: the ToolContext and the @guarded decorator."""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..client import ESClient
from ..config import Settings
from ..errors import ElasticsearchError, ESMCPError, TransportError
from ..safety import Guard

log = logging.getLogger("es_mcp.tools")


@dataclass
class ToolContext:
    """Everything a tool needs: the HTTP client, the policy Guard, and settings.

    ``render`` is the single choke point for output size: tools always return
    ``ctx.render(obj)`` so ``max_response_chars`` is enforced uniformly.
    """

    client: ESClient
    guard: Guard
    settings: Settings

    def render(self, obj: Any) -> str:
        text = obj if isinstance(obj, str) else json.dumps(obj, default=str, ensure_ascii=False)
        limit = self.settings.max_response_chars
        if limit and len(text) > limit:
            return text[:limit] + f"\n... [truncated, {len(text)} chars > {limit} limit]"
        return text


def guarded(
    ctx: ToolContext, name: str
) -> Callable[[Callable[..., Awaitable[str]]], Callable[..., Awaitable[str]]]:
    """Wrap a tool so it returns readable error text instead of raising.

    The model can then correct itself (fix a field name, widen the allow-list)
    rather than seeing an opaque exception. Every call is audited with its
    outcome.
    """

    def decorator(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                result = await fn(*args, **kwargs)
                ctx.guard.audit(name, kwargs, "ok")
                return result
            except (ESMCPError, ElasticsearchError, TransportError) as exc:
                ctx.guard.audit(name, kwargs, f"refused: {exc}")
                return f"ERROR ({name}): {exc}"
            except Exception as exc:  # noqa: BLE001
                log.exception("unexpected failure in %s", name)
                ctx.guard.audit(name, kwargs, f"error: {exc}")
                return f"ERROR ({name}): unexpected {type(exc).__name__}: {exc}"

        return wrapper

    return decorator
