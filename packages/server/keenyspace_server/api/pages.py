from __future__ import annotations

import io
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from keenyspace_shared.mcp_contracts import ReadPageResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.fs.path_safety import UnsafePath, open_workspace_page

router = APIRouter()


@router.get("/{slug}/pages/{path:path}", response_model=ReadPageResponse)
async def get_page(
    slug: str,
    path: str,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> ReadPageResponse:
    result = await session.execute(select(Workspace).where(Workspace.slug == slug))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workspace {slug!r} not found")

    settings = request.app.state.settings
    ws_root = settings.fs.root / "workspaces" / str(ws.uuid)

    try:
        fd, resolved = open_workspace_page(ws_root, path)
    except UnsafePath as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"page {path!r} not found") from exc

    with io.FileIO(fd) as f:
        raw_content = f.read()

    content_str = raw_content.decode("utf-8", errors="replace")
    frontmatter, body = _split_frontmatter(content_str)

    return ReadPageResponse(
        path=str(resolved.relative_to(ws_root)),
        content=body,
        frontmatter=frontmatter,
    )


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content
    yaml_text = content[4:end]
    body = content[end + 5:]
    try:
        fm = yaml.safe_load(yaml_text)
        if not isinstance(fm, dict):
            return {}, content
        return fm, body
    except yaml.YAMLError:
        return {}, content
