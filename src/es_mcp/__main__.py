"""CLI entry point. ``elasticsearch-mcp`` runs this.

Default transport is stdio (for Claude Desktop / Claude Code). Pass
``--transport streamable-http`` for a shared network deployment.
"""

from __future__ import annotations

import argparse

from .server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="elasticsearch-mcp")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http", "sse"],
        help="MCP transport (default: stdio)",
    )
    args = parser.parse_args()

    server = build_server()
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
