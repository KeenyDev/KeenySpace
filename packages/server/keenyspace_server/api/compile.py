from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.api.workspace_dep import require_workspace
from keenyspace_server.compile.models import CompileStatusResponse, CompileTriggerResponse
from keenyspace_server.db.session import get_db

log = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/{slug}/compile", response_model=CompileTriggerResponse, status_code=202)
async def trigger_compile(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> CompileTriggerResponse:
    ws = await require_workspace(session, slug)
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
    try:
        trigger_result: CompileTriggerResponse = await coordinator.trigger(ws.uuid, source="http_api")
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return trigger_result


@router.get("/{slug}/compile/status", response_model=CompileStatusResponse)
async def compile_status(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> CompileStatusResponse:
    ws = await require_workspace(session, slug)
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
    ws = await require_workspace(session, slug)
    if ws.status == "archived":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "workspace_archived",
                "message": f"workspace {slug!r} is archived; unarchive it to resume compile",
            },
        )
    coordinator = request.app.state.compile_coordinator
    if coordinator is None:
        raise HTTPException(status_code=503, detail="compile coordinator not initialised")
    await coordinator.resume(ws.uuid)
    status_result: CompileStatusResponse = await coordinator.status(ws.uuid)
    return status_result
