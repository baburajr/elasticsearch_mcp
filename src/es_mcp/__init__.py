"""elasticsearch-mcp: an MCP server for Elasticsearch over the REST API."""

from __future__ import annotations

from .server import build_server

__version__ = "0.1.0"
__all__ = ["build_server", "__version__"]
