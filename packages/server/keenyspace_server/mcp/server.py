from __future__ import annotations

from fastmcp import FastMCP

from .tools import append_log, ping, read_page


def build_mcp_skeleton() -> FastMCP:
    mcp: FastMCP = FastMCP("KeenySpace-skeleton")
    mcp.add_tool(ping)
    return mcp


def build_mcp() -> FastMCP:
    mcp: FastMCP = FastMCP("KeenySpace")
    mcp.add_tool(read_page)
    mcp.add_tool(append_log)
    return mcp
