from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.compile.models import CompileTriggerResponse
from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db

log = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/{slug}/compile", response_model=CompileTriggerResponse, status_code=202)
async def trigger_compile(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> CompileTriggerResponse:
    db_result = await session.execute(select(Workspace).where(Workspace.slug == slug))
    ws = db_result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workspace {slug!r} not found")

    coordinator = request.app.state.compile_coordinator
    if coordinator is None:
        raise HTTPException(status_code=503, detail="compile coordinator not initialised")

    trigger_result: CompileTriggerResponse = await coordinator.trigger(ws.uuid, source="http_api")
    return trigger_result
