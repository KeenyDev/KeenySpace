from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.auth.audit import write_audit
from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.fs.layout import workspace_root
from keenyspace_server.observability.metrics import WORKSPACE_EXPORT_BYTES_TOTAL
from keenyspace_server.ws.export import (
    ExportTooLargeError,
    build_workspace_zip,
)
from keenyspace_server.ws.registry import workspace_by_slug

log = structlog.get_logger(__name__)
router = APIRouter()


async def _load_workspace(slug: str, session: AsyncSession) -> Workspace:
    ws = await workspace_by_slug(session, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workspace {slug!r} not found")
    return ws


def _resolve_ws_dir(request: Request, ws: Workspace) -> Path:
    settings = request.app.state.settings
    return workspace_root(settings.fs.root, ws.uuid)


@router.get("/{slug}/export")
async def export_workspace(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    user = request.user
    ws = await _load_workspace(slug, session)
    ws_dir = _resolve_ws_dir(request, ws)
    archived = ws.status == "archived"

    audit_payload: dict[str, Any] = {
        "workspace_slug": ws.slug,
        "user_sub": user.sub,
        "archived": archived,
    }
    await write_audit(
        session,
        actor_sub=user.sub,
        action="workspace.exported",
        workspace_uuid=ws.uuid,
        payload=audit_payload,
    )
    await session.commit()

    try:
        chunk_iter = await build_workspace_zip(
            ws_dir, tmp_root=Path(request.app.state.settings.fs.root) / ".tmp"
        )
    except ExportTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "workspace_export_too_large",
                "slug": ws.slug,
                "message": str(exc),
            },
        ) from exc

    ws_slug = ws.slug

    async def _stream() -> Any:
        total = 0
        try:
            async for chunk in chunk_iter:
                total += len(chunk)
                yield chunk
        finally:
            WORKSPACE_EXPORT_BYTES_TOTAL.labels(workspace=ws_slug).inc(total)
            log.info(
                "workspace.exported",
                workspace=str(ws.uuid),
                slug=ws_slug,
                bytes=total,
                archived=archived,
            )

    return StreamingResponse(
        _stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{ws_slug}.zip"'},
    )
