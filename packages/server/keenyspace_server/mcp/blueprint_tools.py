from __future__ import annotations

from typing import Any

from keenyspace_shared.mcp_contracts import Instructions, ListBlueprintsResponse


async def list_blueprints_tool() -> ListBlueprintsResponse:
    """Return blueprint catalog (Phase 4 MCP-08). Stub — Plan 06 fills."""
    raise NotImplementedError("list_blueprints_tool — Plan 06")


async def get_instructions_tool(
    workspace: str,
    command: str,
    context: dict[str, Any],
) -> Instructions:
    """Return Jinja2-rendered instructions (Phase 4 MCP-07). Stub — Plan 06 fills."""
    raise NotImplementedError("get_instructions_tool — Plan 06")
