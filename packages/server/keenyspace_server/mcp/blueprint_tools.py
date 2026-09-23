from __future__ import annotations

import re
from typing import Any

import structlog
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from keenyspace_shared.mcp_contracts import Instructions, ListBlueprintsResponse

from keenyspace_server.db.session import get_db_session
from keenyspace_server.fs.layout import workspace_root
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp, resolve_workspace
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.ws.blueprints import list_blueprints_from_fs
from keenyspace_server.ws.instructions import (
    InstructionNotFoundError,
    InstructionTemplateError,
    load_and_render_instructions,
)
from keenyspace_server.ws.registry import workspace_by_slug

log = structlog.get_logger(__name__)

_COMMAND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


async def list_blueprints_tool() -> ListBlueprintsResponse:
    """List the blueprints this server offers, with name, version and description.

    A blueprint is the starting vault layout a workspace is pinned to; the pin
    of a given workspace is reported by `get_workspace_info`. Takes no
    arguments and is not scoped to a workspace.
    """
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
    """Fetch the server-defined prompt for a workspace command.

    Returns the rendered prompt plus the tools the command may use, its steps,
    an optional model hint, and step/token/time budgets. Which commands exist
    and which context keys they require is defined per workspace; the error
    message names the missing key.

    Fails with `instructions_not_found` when the workspace or the command does
    not exist, and with `instructions_template_error` when the template cannot
    be rendered with the given context.

    Args:
        command: Command name, lowercase, e.g. "query" or "ingest".
        context: Values the command's template expects, e.g. {"question": ...}.
        workspace: Workspace slug. Required unless the MCP connection URL pins
            one as `?workspace=<slug>`; an explicit value always wins.
    """
    with MCP_TOOL_CALL_DURATION.labels(tool="get_instructions").time():
        user = current_user_from_mcp()
        _ = user
        workspace = resolve_workspace(workspace)

        if not _COMMAND_RE.match(command):
            raise ToolError(f"invalid command name {command!r}: must match {_COMMAND_RE.pattern}")

        req = get_http_request()
        settings = req.app.state.settings

        async with get_db_session() as session:
            ws = await workspace_by_slug(session, workspace)

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        ws_dir = workspace_root(settings.fs.root, ws.uuid)
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
