"""Configuration for the Elasticsearch MCP server.

Everything is driven by environment variables prefixed with ``ES_MCP_``.
Defaults are deliberately conservative: read-only, system indices denied,
result sizes capped.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:  # pydantic-settings >= 2.3: stop env values being JSON-decoded before validation
    from pydantic_settings import NoDecode

    CSVList = Annotated[list[str], NoDecode]
except ImportError:  # pragma: no cover
    CSVList = list[str]  # type: ignore[misc,assignment]


def _csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if str(v).strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ES_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- connection -------------------------------------------------------
    hosts: CSVList = Field(default=["http://localhost:9200"])
    api_key: str | None = None
    username: str | None = None
    password: str | None = None
    bearer_token: str | None = None
    verify_certs: bool = True
    ca_certs: str | None = None
    client_cert: str | None = None
    client_key: str | None = None

    request_timeout: float = 30.0
    connect_timeout: float = 10.0
    max_retries: int = 3
    backoff_base: float = 0.25
    max_connections: int = 20

    # --- safety -----------------------------------------------------------
    read_only: bool = True
    """When true every mutating tool (put_mapping, reindex, restore, snapshot) refuses."""

    allow_destructive: bool = False
    """Extra gate on top of read_only for restore / reindex-into-existing."""

    index_allow: CSVList = Field(default=["*"])
    index_deny: CSVList = Field(default=[".*", "security-*"])

    # --- query limits -----------------------------------------------------
    default_size: int = 10
    max_result_size: int = 200
    max_agg_buckets: int = 1000
    search_timeout: str = "30s"
    terminate_after: int | None = None
    max_response_chars: int = 60_000
    max_source_chars: int = 2_000

    # --- observability ----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    audit_log_path: str | None = None
    server_name: str = "elasticsearch"

    @field_validator("hosts", "index_allow", "index_deny", mode="before")
    @classmethod
    def _split(cls, v: object) -> list[str]:
        return _csv(v)  # type: ignore[arg-type]

    @field_validator("hosts")
    @classmethod
    def _need_host(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("ES_MCP_HOSTS must contain at least one URL")
        for h in v:
            if not h.startswith(("http://", "https://")):
                raise ValueError(f"host must include scheme: {h}")
        return [h.rstrip("/") for h in v]

    @property
    def writes_enabled(self) -> bool:
        return not self.read_only


def load_settings() -> Settings:
    return Settings()
