from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.compile.models import CompileStatusResponse, CompileTriggerResponse
from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db

log = structlog.get_logger(__name__)

router = APIRouter()


async def _load_workspace(slug: str, session: AsyncSession) -> Workspace:
    result = await session.execute(select(Workspace).where(Workspace.slug == slug))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workspace {slug!r} not found")
    return ws


@router.post("/{slug}/compile", response_model=CompileTriggerResponse, status_code=202)
async def trigger_compile(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> CompileTriggerResponse:
    ws = await _load_workspace(slug, session)
    if ws.compile_state == "paused":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "workspace_paused",
                "paused_reason": ws.compile_paused_reason,
                "paused_at": str(ws.compile_paused_at) if ws.compile_paused_at else None,
            },
        )
    coordinator = request.app.state.compile_coordinator
    if coordinator is None:
        raise HTTPException(status_code=503, detail="compile coordinator not initialised")
    trigger_result: CompileTriggerResponse = await coordinator.trigger(ws.uuid, source="http_api")
    return trigger_result


@router.get("/{slug}/compile/status", response_model=CompileStatusResponse)
async def compile_status(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> CompileStatusResponse:
    ws = await _load_workspace(slug, session)
    coordinator = request.app.state.compile_coordinator
    if coordinator is None:
        raise HTTPException(status_code=503, detail="compile coordinator not initialised")
    status_result: CompileStatusResponse = await coordinator.status(ws.uuid)
    return status_result


@router.post("/{slug}/compile/resume", response_model=CompileStatusResponse)
async def compile_resume(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> CompileStatusResponse:
    ws = await _load_workspace(slug, session)
    coordinator = request.app.state.compile_coordinator
    if coordinator is None:
        raise HTTPException(status_code=503, detail="compile coordinator not initialised")
    await coordinator.resume(ws.uuid)
    status_result: CompileStatusResponse = await coordinator.status(ws.uuid)
    return status_result
