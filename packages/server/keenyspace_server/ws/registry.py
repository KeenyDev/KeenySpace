"""Lookups against the workspace registry rows in Postgres."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import Workspace


async def workspace_by_slug(session: AsyncSession, slug: str) -> Workspace | None:
    """Return the workspace registered under ``slug``, or None if there is none.

    Archived workspaces are returned like any other; callers that must refuse
    them check ``status`` themselves.
    """
    result = await session.execute(select(Workspace).where(Workspace.slug == slug))
    return result.scalar_one_or_none()
