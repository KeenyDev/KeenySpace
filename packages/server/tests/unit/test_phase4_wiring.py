from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_build_mcp_registers_eleven_tools() -> None:
    from keenyspace_server.mcp.server import build_mcp

    mcp = build_mcp()
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    expected = {
        "read_page",
        "append_log",
        "compile",
        "compile_status",
        "list_workspaces",
        "get_workspace_info",
        "list_pages",
        "search_workspace",
        "get_recent_changes",
        "list_blueprints",
        "get_instructions",
    }
    for name in expected:
        assert name in tool_names, f"{name!r} not in {tool_names}"
    assert len(tool_names) == 11, f"expected 11 tools, got {len(tool_names)}: {tool_names}"


def test_router_stubs_importable() -> None:
    import fastapi
    from keenyspace_server.api.workspace_archive import router as archive_router
    from keenyspace_server.api.workspace_export import router as export_router
    from keenyspace_server.api.workspace_import import router as import_router
    from keenyspace_server.api.workspace_list import router as list_router

    for router in (archive_router, export_router, import_router, list_router):
        assert isinstance(router, fastapi.APIRouter)


def test_mcp_tool_stubs_importable() -> None:
    from keenyspace_server.mcp.blueprint_tools import get_instructions_tool, list_blueprints_tool
    from keenyspace_server.mcp.page_tools import list_pages_tool, search_workspace_tool
    from keenyspace_server.mcp.recent_tool import get_recent_changes_tool
    from keenyspace_server.mcp.workspace_tools import get_workspace_info_tool, list_workspaces_tool

    for fn in (
        list_workspaces_tool,
        get_workspace_info_tool,
        list_pages_tool,
        search_workspace_tool,
        get_recent_changes_tool,
        list_blueprints_tool,
        get_instructions_tool,
    ):
        assert callable(fn), f"{fn!r} is not callable"
