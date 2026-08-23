"""Thin async wrapper over the Elasticsearch REST API.

No official ES client dependency: plain HTTP so the same server works against
Elasticsearch 7/8/9, OpenSearch, and managed offerings that only expose REST.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import ssl
import time
from typing import Any, Mapping

import httpx

from .config import Settings
from .errors import ElasticsearchError, TransportError

log = logging.getLogger("es_mcp.client")

RETRY_STATUS = {429, 502, 503, 504}
IDEMPOTENT = {"GET", "HEAD", "OPTIONS"}


def _auth_headers(s: Settings) -> dict[str, str]:
    if s.api_key:
        return {"Authorization": f"ApiKey {s.api_key}"}
    if s.bearer_token:
        return {"Authorization": f"Bearer {s.bearer_token}"}
    if s.username and s.password:
        raw = f"{s.username}:{s.password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return {}


def _ssl_context(s: Settings) -> ssl.SSLContext | bool:
    if not s.verify_certs:
        return False
    if s.ca_certs:
        ctx = ssl.create_default_context(cafile=s.ca_certs)
        if s.client_cert:
            ctx.load_cert_chain(s.client_cert, s.client_key)
        return ctx
    return True


class ESClient:
    """Connection-pooled client with retry/backoff and host failover."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._hosts = list(settings.hosts)
        self._cursor = 0
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(_auth_headers(settings))
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(settings.request_timeout, connect=settings.connect_timeout),
            verify=_ssl_context(settings),
            limits=httpx.Limits(max_connections=settings.max_connections),
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _next_host(self) -> str:
        host = self._hosts[self._cursor % len(self._hosts)]
        self._cursor += 1
        return host

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        content: str | bytes | None = None,
        timeout: float | None = None,
    ) -> Any:
        method = method.upper()
        path = "/" + path.lstrip("/")
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        # httpx wants primitives
        for k, v in list(clean_params.items()):
            if isinstance(v, bool):
                clean_params[k] = "true" if v else "false"
            elif isinstance(v, (list, tuple)):
                clean_params[k] = ",".join(str(i) for i in v)

        attempts = self.settings.max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            host = self._next_host()
            url = host + path
            started = time.perf_counter()
            try:
                if content is not None:  # NDJSON (_bulk) — raw body, not JSON
                    resp = await self._client.request(
                        method,
                        url,
                        params=clean_params or None,
                        content=content,
                        headers={"Content-Type": "application/x-ndjson"},
                        timeout=timeout or self.settings.request_timeout,
                    )
                else:
                    resp = await self._client.request(
                        method,
                        url,
                        params=clean_params or None,
                        json=body if body is not None else None,
                        timeout=timeout or self.settings.request_timeout,
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                log.warning("transport failure %s %s attempt=%d err=%s", method, path, attempt + 1, exc)
                if attempt + 1 < attempts:
                    await self._sleep(attempt)
                    continue
                raise TransportError(f"{method} {path} failed after {attempts} attempts: {exc}") from exc

            elapsed_ms = (time.perf_counter() - started) * 1000
            log.info("%s %s -> %s in %.0fms", method, path, resp.status_code, elapsed_ms)

            if resp.status_code in RETRY_STATUS and attempt + 1 < attempts:
                if method in IDEMPOTENT or resp.status_code == 429:
                    await self._sleep(attempt, resp.headers.get("Retry-After"))
                    continue

            payload = self._decode(resp)
            if resp.status_code >= 400:
                raise ElasticsearchError(resp.status_code, payload, method, path)
            return payload

        raise TransportError(f"{method} {path} exhausted retries: {last_exc}")

    async def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 10.0))
                return
            except ValueError:
                pass
        delay = self.settings.backoff_base * (2**attempt)
        await asyncio.sleep(min(delay, 8.0) * (0.5 + random.random()))

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            try:
                return resp.json()
            except ValueError:
                return resp.text
        return resp.text

    # --- sugar ------------------------------------------------------------
    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw: Any) -> Any:
        return await self.request("PUT", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def ping(self) -> dict[str, Any]:
        info = await self.get("/")
        version = (info or {}).get("version", {}) if isinstance(info, dict) else {}
        return {
            "cluster_name": info.get("cluster_name") if isinstance(info, dict) else None,
            "version": version.get("number"),
            "distribution": version.get("distribution", "elasticsearch"),
        }
