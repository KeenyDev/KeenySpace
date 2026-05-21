from __future__ import annotations

from keenyspace_shared.mcp_contracts import ListPagesResponse, SearchResponse


async def list_pages_tool(
    workspace: str,
    prefix: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> ListPagesResponse:
    """List pages in workspace (Phase 4 MCP-04). Stub — Plan 04 fills."""
    raise NotImplementedError("list_pages_tool — Plan 04")


async def search_workspace_tool(
    workspace: str,
    query: str,
    cursor: str | None = None,
    limit: int | None = None,
) -> SearchResponse:
    """Search workspace pages by filename + content (Phase 4 MCP-05). Stub — Plan 04 fills."""
    raise NotImplementedError("search_workspace_tool — Plan 04")
