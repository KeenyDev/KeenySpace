from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.api.workspace_dep import require_workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.fs.layout import workspace_root
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
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
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

    try:
        ws = await require_workspace(session, slug)
    except HTTPException:
        WORKSPACE_MANIFEST_TOTAL.labels(outcome="not_found").inc()
        raise

    settings = request.app.state.settings
    ws_root = workspace_root(settings.fs.root, ws.uuid)
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


def _safe_workspace_relative(ws_root: Path, rel: str) -> Path | None:
    if not rel or "\x00" in rel or len(rel) > 1024:
        return None
    if rel.startswith("/") or rel.startswith("\\"):
        return None
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] in _EXCLUDED_TOP_LEVEL:
        return None
    if not (rel.endswith(".md") or parts[0] == "raw"):
        return None
    candidate = (ws_root / rel).resolve()
    try:
        candidate.relative_to(ws_root.resolve())
    except ValueError:
        return None
    return candidate


def _resolve_raw_file(ws_root: Path, rel: str) -> Path | None:
    target = _safe_workspace_relative(ws_root, rel)
    if target is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_path"})
    return target if target.is_file() else None


@router.get("/{slug}/pages-raw/{path:path}")
async def workspace_page_raw(
    slug: str,
    path: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> FileResponse:
    """Raw bytes for a single file inside the pull-scope (.md or raw/*).

    The /pages/{path} endpoint returns ReadPageResponse JSON (parsed
    frontmatter + body), which cannot be compared byte-for-byte against a
    local working copy. This endpoint returns the file bytes verbatim with an
    octet-stream content-type. Its scope is restricted to the same set of
    files as the manifest endpoint, so it cannot read anything the manifest
    would not list.
    """

    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail={"error": "invalid_slug"})

    ws = await require_workspace(session, slug)

    settings = request.app.state.settings
    ws_root = workspace_root(settings.fs.root, ws.uuid)
    target = await asyncio.to_thread(_resolve_raw_file, ws_root, path)
    if target is None:
        raise HTTPException(status_code=404, detail=f"path {path!r} not found")
    return FileResponse(target, media_type="application/octet-stream")
