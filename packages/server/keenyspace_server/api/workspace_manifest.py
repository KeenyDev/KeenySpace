from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.observability.metrics import WORKSPACE_MANIFEST_TOTAL

log = structlog.get_logger(__name__)
router = APIRouter()

_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")
_EXCLUDED_TOP_LEVEL = frozenset({".obsidian", ".keenyspace", "logs", "tmp"})


def _scan_workspace(ws_root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not ws_root.is_dir():
        return files
    for path in ws_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ws_root).as_posix()
        parts = rel.split("/")
        if parts[0] in _EXCLUDED_TOP_LEVEL:
            continue
        if not (rel.endswith(".md") or parts[0] == "raw"):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[rel] = f"sha256:{digest}"
    return files


@router.get("/{slug}/manifest")
async def workspace_manifest(
    slug: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    if not _SLUG_RE.match(slug):
        WORKSPACE_MANIFEST_TOTAL.labels(outcome="invalid_slug").inc()
        raise HTTPException(status_code=400, detail={"error": "invalid_slug"})

    result = await session.execute(select(Workspace).where(Workspace.slug == slug))
    ws = result.scalar_one_or_none()
    if ws is None:
        WORKSPACE_MANIFEST_TOTAL.labels(outcome="not_found").inc()
        raise HTTPException(status_code=404, detail=f"workspace {slug!r} not found")

    settings = request.app.state.settings
    ws_root = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
    files = await asyncio.to_thread(_scan_workspace, ws_root)
    WORKSPACE_MANIFEST_TOTAL.labels(outcome="success").inc()
    log.info(
        "workspace.manifest.served",
        workspace_slug=ws.slug,
        file_count=len(files),
    )
    return {
        "files": files,
        "server_canon_at": datetime.now(UTC).isoformat(),
    }
