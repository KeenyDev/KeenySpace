from __future__ import annotations

import re
from typing import Any

import structlog
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from keenyspace_shared.mcp_contracts import Instructions, ListBlueprintsResponse
from sqlalchemy import select

from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db_session
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp, resolve_workspace
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.ws.blueprints import list_blueprints_from_fs
from keenyspace_server.ws.instructions import (
    InstructionNotFoundError,
    InstructionTemplateError,
    load_and_render_instructions,
)

log = structlog.get_logger(__name__)

_COMMAND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


async def list_blueprints_tool() -> ListBlueprintsResponse:
    with MCP_TOOL_CALL_DURATION.labels(tool="list_blueprints").time():
        user = current_user_from_mcp()
        _ = user
        req = get_http_request()
        settings = req.app.state.settings
        fs_root = settings.fs.root
        blueprints = await list_blueprints_from_fs(fs_root)
        return ListBlueprintsResponse(blueprints=blueprints)


async def get_instructions_tool(
    command: str,
    context: dict[str, Any],
    workspace: str | None = None,
) -> Instructions:
    with MCP_TOOL_CALL_DURATION.labels(tool="get_instructions").time():
        user = current_user_from_mcp()
        _ = user
        workspace = resolve_workspace(workspace)

        if not _COMMAND_RE.match(command):
            raise ToolError(
                f"invalid command name {command!r}: must match {_COMMAND_RE.pattern}"
            )

        req = get_http_request()
        settings = req.app.state.settings

        async with get_db_session() as session:
            result = await session.execute(
                select(Workspace).where(Workspace.slug == workspace)
            )
            ws = result.scalar_one_or_none()

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        ws_dir = settings.fs.root / "workspaces" / str(ws.uuid)
        workspace_meta: dict[str, Any] = {
            "uuid": str(ws.uuid),
            "slug": ws.slug,
            "blueprint_pin": ws.blueprint_ref,
        }
        try:
            return await load_and_render_instructions(
                ws_dir,
                command=command,
                workspace_meta=workspace_meta,
                context=context,
            )
        except InstructionNotFoundError as exc:
            raise ToolError(f"instructions_not_found: {exc}") from exc
        except InstructionTemplateError as exc:
            raise ToolError(f"instructions_template_error: {exc}") from exc
