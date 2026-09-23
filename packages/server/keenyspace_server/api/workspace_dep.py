"""Transport-level workspace lookup shared by the workspace-scoped routers."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import Workspace
from keenyspace_server.ws.registry import workspace_by_slug


async def require_workspace(session: AsyncSession, slug: str) -> Workspace:
    """Return the workspace registered under ``slug``.

    Raises HTTPException(404) — and nothing else — when no such workspace
    exists. Archived workspaces are returned like any other; callers that must
    refuse them check ``status`` themselves.
    """
    ws = await workspace_by_slug(session, slug)
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workspace {slug!r} not found")
    return ws
