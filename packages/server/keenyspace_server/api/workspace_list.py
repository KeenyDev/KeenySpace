from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastmcp.utilities.pagination import paginate_sequence
from keenyspace_shared.mcp_contracts import (
    ListWorkspacesResponse,
    WorkspaceInfo,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import CompileRun, Workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.ws.scan import iter_md_files

log = structlog.get_logger(__name__)
router = APIRouter()

_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MAX = 200


def _validated_limit(limit: int | None) -> int:
    if limit is None:
        return _PAGE_SIZE_DEFAULT
    return min(max(1, limit), _PAGE_SIZE_MAX)


def _count_pages_sync(ws_dir: Path) -> int:
    if not ws_dir.is_dir():
        return 0
    return sum(1 for _ in iter_md_files(ws_dir))


async def _build_workspace_info(
    ws: Workspace, ws_dir: Path, session: AsyncSession
) -> WorkspaceInfo:
    page_count = await asyncio.to_thread(_count_pages_sync, ws_dir)
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


@router.get("/", response_model=ListWorkspacesResponse)
async def list_workspaces_http(
    request: Request,
    status: Annotated[Literal["active", "archived", "all"], Query()] = "active",
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=_PAGE_SIZE_MAX)] = None,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> ListWorkspacesResponse:
    stmt = select(Workspace)
    if status == "active":
        stmt = stmt.where(Workspace.status == "active")
    elif status == "archived":
        stmt = stmt.where(Workspace.status == "archived")
    stmt = stmt.order_by(Workspace.slug)
    rows = list((await session.execute(stmt)).scalars().all())

    page_size = _validated_limit(limit)
    try:
        page_rows, next_cursor = paginate_sequence(rows, cursor=cursor, page_size=page_size)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"malformed cursor: {exc}") from exc

    settings = request.app.state.settings
    infos: list[WorkspaceInfo] = []
    for ws in page_rows:
        ws_dir = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
        infos.append(await _build_workspace_info(ws, ws_dir, session))

    return ListWorkspacesResponse(workspaces=infos, next_cursor=next_cursor)


@router.get("/{slug}", response_model=WorkspaceInfo)
async def get_workspace_http(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> WorkspaceInfo:
    ws = (
        await session.execute(select(Workspace).where(Workspace.slug == slug))
    ).scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workspace {slug!r} not found")
    settings = request.app.state.settings
    ws_dir = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
    return await _build_workspace_info(ws, ws_dir, session)
