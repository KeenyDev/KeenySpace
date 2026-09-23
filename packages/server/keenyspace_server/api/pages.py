from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from keenyspace_shared.mcp_contracts import ReadPageResponse
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.api.workspace_dep import require_workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.fs.layout import workspace_root
from keenyspace_server.fs.path_safety import UnsafePath, open_workspace_page
from keenyspace_server.ws.frontmatter import split_frontmatter

router = APIRouter()


@router.get("/{slug}/pages/{path:path}", response_model=ReadPageResponse)
async def get_page(
    slug: str,
    path: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> ReadPageResponse:
    ws = await require_workspace(session, slug)

    settings = request.app.state.settings
    ws_root = workspace_root(settings.fs.root, ws.uuid)

    try:
        return await asyncio.to_thread(_read_page_sync, ws_root, path)
    except UnsafePath as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"page {path!r} not found") from exc


def _read_page_sync(ws_root: Path, path: str) -> ReadPageResponse:
    fd, resolved = open_workspace_page(ws_root, path)
    with io.FileIO(fd) as f:
        raw_content = f.read()

    content_str = raw_content.decode("utf-8", errors="replace")
    frontmatter, body = split_frontmatter(content_str)

    return ReadPageResponse(
        path=str(resolved.relative_to(ws_root)),
        content=body,
        frontmatter=frontmatter,
    )
