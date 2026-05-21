from __future__ import annotations

from fastmcp import FastMCP

from .blueprint_tools import get_instructions_tool, list_blueprints_tool
from .page_tools import list_pages_tool, search_workspace_tool
from .recent_tool import get_recent_changes_tool
from .tools import append_log, compile_status_tool, compile_tool, ping, read_page
from .workspace_tools import get_workspace_info_tool, list_workspaces_tool


def build_mcp_skeleton() -> FastMCP:
    mcp: FastMCP = FastMCP("KeenySpace-skeleton")
    mcp.add_tool(ping)
    return mcp


def build_mcp() -> FastMCP:
    mcp: FastMCP = FastMCP("KeenySpace")
    mcp.add_tool(read_page)
    mcp.add_tool(append_log)
    mcp.add_tool(compile_tool)
    mcp.add_tool(compile_status_tool)
    mcp.add_tool(list_workspaces_tool)
    mcp.add_tool(get_workspace_info_tool)
    mcp.add_tool(list_pages_tool)
    mcp.add_tool(search_workspace_tool)
    mcp.add_tool(get_recent_changes_tool)
    mcp.add_tool(list_blueprints_tool)
    mcp.add_tool(get_instructions_tool)
    return mcp
