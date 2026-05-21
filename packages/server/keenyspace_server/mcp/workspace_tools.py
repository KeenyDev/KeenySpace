from __future__ import annotations

from keenyspace_shared.mcp_contracts import ListWorkspacesResponse, WorkspaceInfo


async def list_workspaces_tool(include_archived: bool = False) -> ListWorkspacesResponse:
    """Return workspaces visible to the caller (Phase 4 MCP-01). Stub — Plan 03 fills."""
    raise NotImplementedError("list_workspaces_tool — Plan 03")


async def get_workspace_info_tool(workspace: str) -> WorkspaceInfo:
    """Return metadata for a workspace (Phase 4 MCP-02). Stub — Plan 03 fills."""
    raise NotImplementedError("get_workspace_info_tool — Plan 03")
