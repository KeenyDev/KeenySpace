from __future__ import annotations

import contextlib
import io
from datetime import UTC, datetime
from typing import Any

import yaml
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from keenyspace_shared.mcp_contracts import AppendLogResponse, ReadPageResponse
from sqlalchemy import select

from keenyspace_server.compile.coordinator import get_coordinator
from keenyspace_server.compile.models import CompileStatusResponse, CompileTriggerResponse
from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db_session
from keenyspace_server.fs.path_safety import UnsafePath, open_workspace_page
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.wal import writer as wal_writer


async def ping(message: str) -> str:
    return f"pong: {message}"


async def read_page(workspace: str, path: str) -> ReadPageResponse:
    with MCP_TOOL_CALL_DURATION.labels(tool="read_page").time():
        user = current_user_from_mcp()
        _ = user

        req = get_http_request()
        app = req.app

        async with get_db_session() as session:
            result = await session.execute(
                select(Workspace).where(Workspace.slug == workspace)
            )
            ws = result.scalar_one_or_none()

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        settings = app.state.settings
        ws_root = settings.fs.root / "workspaces" / str(ws.uuid)

        try:
            fd, resolved = open_workspace_page(ws_root, path)
        except UnsafePath as exc:
            raise ToolError(f"400 Bad Request: {exc}") from exc
        except FileNotFoundError as exc:
            raise ToolError(f"page {path!r} not found in workspace {workspace!r}") from exc

        with io.FileIO(fd) as f:
            raw_content = f.read()

        content_str = raw_content.decode("utf-8", errors="replace")
        frontmatter, body = _split_frontmatter(content_str)

        return ReadPageResponse(
            path=str(resolved.relative_to(ws_root)),
            content=body,
            frontmatter=frontmatter,
        )


async def append_log(
    workspace: str,
    content: str,
    parent_id: str | None = None,
) -> AppendLogResponse:
    with MCP_TOOL_CALL_DURATION.labels(tool="append_log").time():
        user = current_user_from_mcp()

        req = get_http_request()
        app = req.app

        async with get_db_session() as session:
            result = await session.execute(
                select(Workspace).where(Workspace.slug == workspace)
            )
            ws = result.scalar_one_or_none()

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        settings = app.state.settings
        ws_root = settings.fs.root / "workspaces" / str(ws.uuid)
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

        from ulid import ULID as _ULID
        parent_ulid: _ULID | None = None
        if parent_id is not None:
            with contextlib.suppress(Exception):
                parent_ulid = _ULID.from_str(parent_id)

        entry_id = await wal_writer.append_log(
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

        return AppendLogResponse(
            entry_id=str(entry_id),
            ts=datetime.now(UTC),
        )


async def compile_tool(workspace: str) -> CompileTriggerResponse:
    """Trigger a compile pass for the workspace. Fire-and-forget; poll compile_status for state."""
    with MCP_TOOL_CALL_DURATION.labels(tool="compile").time():
        user = current_user_from_mcp()
        _ = user

        async with get_db_session() as session:
            result = await session.execute(select(Workspace).where(Workspace.slug == workspace))
            ws = result.scalar_one_or_none()

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


async def compile_status_tool(workspace: str) -> CompileStatusResponse:
    """Return current compile state for a workspace."""
    with MCP_TOOL_CALL_DURATION.labels(tool="compile_status").time():
        user = current_user_from_mcp()
        _ = user

        async with get_db_session() as session:
            result = await session.execute(select(Workspace).where(Workspace.slug == workspace))
            ws = result.scalar_one_or_none()

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        coordinator = get_coordinator()
        if coordinator is None:
            raise ToolError("compile coordinator not initialised")
        status_result: CompileStatusResponse = await coordinator.status(ws.uuid)
        return status_result


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content

    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content

    yaml_text = content[4:end]
    body = content[end + 5:]

    try:
        fm = yaml.safe_load(yaml_text)
        if not isinstance(fm, dict):
            return {}, content
        return fm, body
    except yaml.YAMLError:
        return {}, content
