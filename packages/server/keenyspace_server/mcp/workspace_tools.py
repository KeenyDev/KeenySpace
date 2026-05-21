from __future__ import annotations

import asyncio
from pathlib import Path

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from keenyspace_shared.mcp_contracts import (
    ListWorkspacesResponse,
    WorkspaceInfo,
)
from sqlalchemy import select

from keenyspace_server.db.models import CompileRun, Workspace
from keenyspace_server.db.session import get_db_session
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.ws.scan import iter_md_files


def _count_pages_sync(ws_dir: Path) -> int:
    if not ws_dir.is_dir():
        return 0
    return sum(1 for _ in iter_md_files(ws_dir))


async def _build_workspace_info(ws: Workspace, ws_dir: Path) -> WorkspaceInfo:
    page_count = await asyncio.to_thread(_count_pages_sync, ws_dir)
    async with get_db_session() as session:
        last_compile_at = (
            await session.execute(
                select(CompileRun.completed_at)
                .where(
                    CompileRun.workspace_uuid == ws.uuid,
                    CompileRun.status == "success",
                )
                .order_by(CompileRun.completed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return WorkspaceInfo(
        uuid=str(ws.uuid),
        slug=ws.slug,
        status=ws.status,
        blueprint_pin=ws.blueprint_ref,
        archived_at=ws.archived_at,
        compile_state=ws.compile_state,
        page_count=page_count,
        last_compile_at=last_compile_at,
    )


async def list_workspaces_tool(include_archived: bool = False) -> ListWorkspacesResponse:
    """Return workspaces visible to caller (MCP-01).

    D-02: archived workspaces hidden by default; opt-in via include_archived=True.
    """
    with MCP_TOOL_CALL_DURATION.labels(tool="list_workspaces_tool").time():
        _ = current_user_from_mcp()

        req = get_http_request()
        app = req.app
        settings = app.state.settings

        stmt = select(Workspace)
        if not include_archived:
            stmt = stmt.where(Workspace.status == "active")
        stmt = stmt.order_by(Workspace.slug)
        async with get_db_session() as session:
            rows = (await session.execute(stmt)).scalars().all()

        infos: list[WorkspaceInfo] = []
        for ws in rows:
            ws_dir = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
            infos.append(await _build_workspace_info(ws, ws_dir))

        return ListWorkspacesResponse(workspaces=infos, next_cursor=None)


async def get_workspace_info_tool(workspace: str) -> WorkspaceInfo:
    """Return metadata for a workspace (MCP-02)."""
    with MCP_TOOL_CALL_DURATION.labels(tool="get_workspace_info_tool").time():
        _ = current_user_from_mcp()

        req = get_http_request()
        app = req.app
        settings = app.state.settings

        async with get_db_session() as session:
            ws = (
                await session.execute(
                    select(Workspace).where(Workspace.slug == workspace)
                )
            ).scalar_one_or_none()

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        ws_dir = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
        return await _build_workspace_info(ws, ws_dir)
