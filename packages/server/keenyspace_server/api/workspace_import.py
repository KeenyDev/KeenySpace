from __future__ import annotations

import secrets
from pathlib import Path

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from keenyspace_shared.mcp_contracts import WorkspaceImportResponse
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.session import get_db
from keenyspace_server.ws.import_ import (
    WorkspaceImportError,
    WorkspaceSlugConflictError,
    import_workspace,
)

log = structlog.get_logger(__name__)
router = APIRouter()

_UPLOAD_CHUNK_BYTES = 64 * 1024


@router.post("/import", response_model=WorkspaceImportResponse, status_code=201)
async def import_endpoint(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    slug: str = Form(...),
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> WorkspaceImportResponse:
    user = request.state.user
    settings = request.app.state.settings

    fs_root: Path = settings.fs.root
    workspaces_dir = fs_root / "workspaces"
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    # Dedicated sibling tmp dir keeps ephemeral upload/import scratch out of
    # `workspaces/` (which must contain only UUID directories). Same fs_root
    # mount, so os.rename to workspaces/<uuid>/ stays atomic.
    tmp_root = fs_root / ".tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    upload_tmp = tmp_root / f"upload_{secrets.token_hex(8)}.zip"

    try:
        with upload_tmp.open("wb") as f:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                f.write(chunk)

        try:
            response = await import_workspace(
                session,
                settings=settings,
                slug=slug,
                zip_path=upload_tmp,
                actor_sub=user.sub,
            )
        except WorkspaceImportError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        except WorkspaceSlugConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "workspace_slug_conflict",
                    "slug": exc.slug,
                },
            ) from exc

        return response
    finally:
        try:
            upload_tmp.unlink(missing_ok=True)
        except Exception as exc:
            log.warning(
                "workspace.import.upload_tmp_cleanup_failed",
                path=str(upload_tmp),
                error=str(exc),
            )
