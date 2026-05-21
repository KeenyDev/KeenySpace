from __future__ import annotations

from keenyspace_shared.mcp_contracts import RecentChangesResponse


async def get_recent_changes_tool(
    workspace: str,
    since: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> RecentChangesResponse:
    """Return pages updated since cursor (Phase 4 MCP-09). Stub — Plan 05 fills."""
    raise NotImplementedError("get_recent_changes_tool — Plan 05")
