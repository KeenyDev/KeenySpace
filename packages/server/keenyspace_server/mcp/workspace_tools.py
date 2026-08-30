from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from keenyspace_shared.mcp_contracts import (
    ListWorkspacesResponse,
    WorkspaceInfo,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import CompileRun, Workspace
from keenyspace_server.db.session import get_db_session
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp, resolve_workspace
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.ws.scan import iter_md_files


def _count_pages_sync(ws_dir: Path) -> int:
    if not ws_dir.is_dir():
        return 0
    return sum(1 for _ in iter_md_files(ws_dir))


async def _fetch_last_compile_map(
    session: AsyncSession, ws_uuids: list[UUID]
) -> dict[UUID, datetime | None]:
    """Batch-fetch last successful compile completion per workspace (WR-11)."""
    if not ws_uuids:
        return {}
    rows = (
        await session.execute(
            select(
                CompileRun.workspace_uuid,
                func.max(CompileRun.completed_at),
            )
            .where(
                CompileRun.workspace_uuid.in_(ws_uuids),
                CompileRun.status == "success",
            )
            .group_by(CompileRun.workspace_uuid)
        )
    ).all()
    found: dict[UUID, datetime | None] = {  # noqa: C416 - Row is not a tuple for mypy
        ws_uuid: completed for ws_uuid, completed in rows
    }
    return {uuid_: found.get(uuid_) for uuid_ in ws_uuids}


def _info_from_parts(
    ws: Workspace, page_count: int, last_compile_at: datetime | None
) -> WorkspaceInfo:
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


async def _build_workspace_info(ws: Workspace, ws_dir: Path) -> WorkspaceInfo:
    """Single-workspace info builder (still used by get_workspace_info_tool)."""
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
    return _info_from_parts(ws, page_count, last_compile_at)


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

        # WR-11: do all DB work in a single session (one SELECT for workspaces
        # + one grouped SELECT for last_compile_at), then parallelize the
        # thread-bound _count_pages_sync calls. Avoids N+1 + session-per-row
        # pool acquisitions that previously serialized at the asyncpg pool.
        async with get_db_session() as session:
            rows = list((await session.execute(stmt)).scalars().all())
            last_compile_map = await _fetch_last_compile_map(
                session, [ws.uuid for ws in rows]
            )

        fs_root = Path(settings.fs.root)
        page_counts = await asyncio.gather(
            *[
                asyncio.to_thread(
                    _count_pages_sync, fs_root / "workspaces" / str(ws.uuid)
                )
                for ws in rows
            ]
        )
        infos: list[WorkspaceInfo] = [
            _info_from_parts(ws, page_count, last_compile_map.get(ws.uuid))
            for ws, page_count in zip(rows, page_counts, strict=True)
        ]

        return ListWorkspacesResponse(workspaces=infos, next_cursor=None)


async def get_workspace_info_tool(workspace: str | None = None) -> WorkspaceInfo:
    """Return metadata for a workspace (MCP-02)."""
    with MCP_TOOL_CALL_DURATION.labels(tool="get_workspace_info_tool").time():
        _ = current_user_from_mcp()
        workspace = resolve_workspace(workspace)

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
