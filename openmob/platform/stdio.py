"""`openmob mcp`: a local stdio MCP server that forwards every call to a server's remote /mcp.

For clients that only launch local commands (Claude Desktop, older IDE plugins). It holds no tools
of its own: tools/list and tools/call go to the remote server as they are, with the API key, so the
remote server stays the single source of truth for tools, schemas and every rule.
"""

from __future__ import annotations

import sys

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared._httpx_utils import create_mcp_http_client


def mcp_url(server: str) -> str:
    """https://host → https://host/mcp (a URL that already ends in /mcp is kept)."""
    server = server.rstrip("/")
    return server if server.endswith("/mcp") else f"{server}/mcp"


async def serve(server: str, key: str) -> None:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {key}", "User-Agent": "openmob-mcp-stdio/1"})
    async with http, Client(streamable_http_client(mcp_url(server), http_client=http)) as remote:

        async def list_tools(ctx, params):
            return await remote.list_tools(cursor=params.cursor if params else None)

        async def call_tool(ctx, params):
            async def progress(done, total, message):  # pass the remote's progress on to the local client
                try:
                    await ctx.session.report_progress(done, total, message)
                except Exception:
                    pass

            return await remote.call_tool(params.name, params.arguments or {}, progress_callback=progress)

        local = Server("OpenMob", instructions=remote.instructions, on_list_tools=list_tools, on_call_tool=call_tool)
        async with stdio_server() as (read, write):
            await local.run(read, write, local.create_initialization_options())


def main(server: str | None, key: str | None) -> None:
    import anyio

    if not server or not key:
        missing = "--server (or $OPENMOB_SERVER)" if not server else "an API key: $OPENMOB_API_KEY or --key"
        print(f"openmob mcp: missing {missing}.", file=sys.stderr)
        sys.exit(2)
    try:
        anyio.run(serve, server, key)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # stdout belongs to the MCP protocol: report on stderr
        print(f"openmob mcp: couldn't reach {mcp_url(server)}: {exc}", file=sys.stderr)
        sys.exit(1)
