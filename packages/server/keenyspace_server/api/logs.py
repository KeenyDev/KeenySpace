from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from keenyspace_shared.mcp_contracts import AppendLogRequest, AppendLogResponse
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from keenyspace_server.api.workspace_dep import require_workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.fs.layout import workspace_root
from keenyspace_server.wal import writer as wal_writer

router = APIRouter()


@router.post("/{slug}/logs", response_model=AppendLogResponse, status_code=201)
async def append_log_endpoint(
    slug: str,
    body: AppendLogRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> AppendLogResponse:
    ws = await require_workspace(session, slug)

    settings = request.app.state.settings
    ws_root = workspace_root(settings.fs.root, ws.uuid)
    locks = request.app.state.wal_locks

    actor_sub = request.user.identity if request.user.is_authenticated else "anonymous"
    actor = f"dev:{actor_sub}"

    client_version: str | None = None
    ua = request.headers.get("user-agent")
    if ua:
        client_version = ua[:64]

    parent_ulid: ULID | None = None
    if body.parent_id is not None:
        try:
            parent_ulid = ULID.from_str(body.parent_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid parent_id: {exc}") from exc

    try:
        appended = await wal_writer.append_log(
            ws_uuid=ws.uuid,
            ws_root=ws_root,
            content=body.content,
            actor=actor,
            source="api",
            client_version=client_version,
            parent_id=parent_ulid,
            settings=settings,
            locks=locks,
        )
    except wal_writer.PayloadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except wal_writer.WorkspaceArchivedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except wal_writer.EmptyContentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return AppendLogResponse(entry_id=str(appended.entry_id), ts=appended.ts)
