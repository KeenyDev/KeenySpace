from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.fs.layout import workspace_root
from keenyspace_server.ws.archive import (
    ArchiveConflictError,
    archive_workspace,
    unarchive_workspace,
)
from keenyspace_server.ws.registry import workspace_by_slug

log = structlog.get_logger(__name__)
router = APIRouter()


class ArchiveResponse(BaseModel):
    slug: str
    status: str
    archived_at: str | None = None


async def _load_workspace(slug: str, session: AsyncSession) -> Workspace:
    ws = await workspace_by_slug(session, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workspace {slug!r} not found")
    return ws


def _resolve_ws_dir(request: Request, ws: Workspace) -> Path:
    settings = request.app.state.settings
    return workspace_root(settings.fs.root, ws.uuid)


@router.post("/{slug}/archive", response_model=ArchiveResponse, status_code=200)
async def archive_endpoint(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> ArchiveResponse:
    user = request.user
    ws = await _load_workspace(slug, session)
    ws_dir = _resolve_ws_dir(request, ws)
    try:
        archived_at = await archive_workspace(
            session,
            ws_uuid=ws.uuid,
            ws_dir=ws_dir,
            actor_sub=user.sub,
            slug=ws.slug,
        )
    except ArchiveConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "workspace_already_archived", "slug": slug, "message": str(exc)},
        ) from exc
    return ArchiveResponse(
        slug=ws.slug,
        status="archived",
        archived_at=archived_at.isoformat(),
    )


@router.post("/{slug}/unarchive", response_model=ArchiveResponse, status_code=200)
async def unarchive_endpoint(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> ArchiveResponse:
    user = request.user
    ws = await _load_workspace(slug, session)
    ws_dir = _resolve_ws_dir(request, ws)
    try:
        await unarchive_workspace(
            session,
            ws_uuid=ws.uuid,
            ws_dir=ws_dir,
            actor_sub=user.sub,
            slug=ws.slug,
        )
    except ArchiveConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "workspace_not_archived", "slug": slug, "message": str(exc)},
        ) from exc
    return ArchiveResponse(slug=ws.slug, status="active", archived_at=None)
