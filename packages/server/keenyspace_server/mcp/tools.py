from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from keenyspace_shared.mcp_contracts import AppendLogResponse, ReadPageResponse
from ulid import ULID

from keenyspace_server.compile.coordinator import get_coordinator
from keenyspace_server.compile.models import CompileStatusResponse, CompileTriggerResponse
from keenyspace_server.db.session import get_db_session
from keenyspace_server.fs.layout import workspace_root
from keenyspace_server.fs.path_safety import UnsafePath, open_workspace_page
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp, resolve_workspace
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.wal import writer as wal_writer
from keenyspace_server.ws.frontmatter import split_frontmatter
from keenyspace_server.ws.registry import workspace_by_slug


async def ping(message: str) -> str:
    return f"pong: {message}"


async def read_page(path: str, workspace: str | None = None) -> ReadPageResponse:
    with MCP_TOOL_CALL_DURATION.labels(tool="read_page").time():
        user = current_user_from_mcp()
        _ = user
        workspace = resolve_workspace(workspace)

        req = get_http_request()
        app = req.app

        async with get_db_session() as session:
            ws = await workspace_by_slug(session, workspace)

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        settings = app.state.settings
        ws_root = workspace_root(settings.fs.root, ws.uuid)

        try:
            return await asyncio.to_thread(_read_page_blocking, ws_root, path)
        except UnsafePath as exc:
            raise ToolError(f"400 Bad Request: {exc}") from exc
        except FileNotFoundError as exc:
            raise ToolError(f"page {path!r} not found in workspace {workspace!r}") from exc


def _read_page_blocking(ws_root: Path, path: str) -> ReadPageResponse:
    fd, resolved = open_workspace_page(ws_root, path)
    with io.FileIO(fd) as f:
        raw_content = f.read()

    frontmatter, body = split_frontmatter(raw_content.decode("utf-8", errors="replace"))
    return ReadPageResponse(
        path=str(resolved.relative_to(ws_root)),
        content=body,
        frontmatter=frontmatter,
    )


async def append_log(
    content: str,
    parent_id: str | None = None,
    workspace: str | None = None,
) -> AppendLogResponse:
    with MCP_TOOL_CALL_DURATION.labels(tool="append_log").time():
        user = current_user_from_mcp()
        workspace = resolve_workspace(workspace)

        req = get_http_request()
        app = req.app

        async with get_db_session() as session:
            ws = await workspace_by_slug(session, workspace)

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        settings = app.state.settings
        ws_root = workspace_root(settings.fs.root, ws.uuid)
        locks = app.state.wal_locks

        from keenyspace_server.auth.user import User
        actor = f"dev:{user.sub}" if isinstance(user, User) else f"unknown:{user.identity}"

        client_version: str | None = None
        try:
            ua = req.headers.get("user-agent")
            if ua:
                client_version = ua[:64]
        except Exception:
            pass

        parent_ulid: ULID | None = None
        if parent_id is not None:
            try:
                parent_ulid = ULID.from_str(parent_id)
            except ValueError as exc:
                raise ToolError(f"invalid parent_id: {exc}") from exc

        try:
            appended = await wal_writer.append_log(
                ws_uuid=ws.uuid,
                ws_root=ws_root,
                content=content,
                actor=actor,
                source="mcp",
                client_version=client_version,
                parent_id=parent_ulid,
                settings=settings,
                locks=locks,
            )
        except (
            wal_writer.PayloadTooLarge,
            wal_writer.WorkspaceArchivedError,
            wal_writer.EmptyContentError,
        ) as exc:
            raise ToolError(str(exc)) from exc

        return AppendLogResponse(entry_id=str(appended.entry_id), ts=appended.ts)


async def compile_tool(workspace: str | None = None) -> CompileTriggerResponse:
    """Trigger a compile pass for the workspace. Fire-and-forget; poll compile_status for state."""
    with MCP_TOOL_CALL_DURATION.labels(tool="compile").time():
        user = current_user_from_mcp()
        _ = user
        workspace = resolve_workspace(workspace)

        async with get_db_session() as session:
            ws = await workspace_by_slug(session, workspace)

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")
        if ws.compile_state == "paused":
            raise ToolError(
                f"workspace {workspace!r} is paused; "
                f"reason={ws.compile_paused_reason!r}; "
                f"call POST /v1/api/workspaces/{workspace}/compile/resume to clear"
            )

        # Use the module-global coordinator (set in app_lifespan via
        # set_coordinator), not request.app.state: under the mounted FastMCP
        # app the request's app.state is not the root app's, so
        # app.state.compile_coordinator is absent there. The REST endpoints run
        # on the root app and correctly read app.state.
        coordinator = get_coordinator()
        if coordinator is None:
            raise ToolError("compile coordinator not initialised")
        try:
            trigger_result: CompileTriggerResponse = await coordinator.trigger(ws.uuid, source="mcp_tool")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return trigger_result


async def compile_status_tool(workspace: str | None = None) -> CompileStatusResponse:
    """Return current compile state for a workspace."""
    with MCP_TOOL_CALL_DURATION.labels(tool="compile_status").time():
        user = current_user_from_mcp()
        _ = user
        workspace = resolve_workspace(workspace)

        async with get_db_session() as session:
            ws = await workspace_by_slug(session, workspace)

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        coordinator = get_coordinator()
        if coordinator is None:
            raise ToolError("compile coordinator not initialised")
        status_result: CompileStatusResponse = await coordinator.status(ws.uuid)
        return status_result
