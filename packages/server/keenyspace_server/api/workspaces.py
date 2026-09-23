from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.fs.blueprint import (
    BLUEPRINT_NAME_PATTERN,
    InvalidBlueprintNameError,
    UnknownBlueprintError,
    clone_default_blueprint,
)
from keenyspace_server.ws.registry import workspace_by_slug

logger = structlog.get_logger(__name__)

router = APIRouter()

_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


class WorkspaceCreateRequest(BaseModel):
    slug: str
    blueprint: str = Field(default="default", pattern=BLUEPRINT_NAME_PATTERN)


class WorkspaceResponse(BaseModel):
    uuid: str
    slug: str
    blueprint_ref: str
    created_at: datetime
    model_config = {"arbitrary_types_allowed": True}


@router.post("/", response_model=WorkspaceResponse, status_code=201)
async def create_workspace(
    body: WorkspaceCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> WorkspaceResponse:
    if not _SLUG_RE.match(body.slug):
        raise HTTPException(
            status_code=422,
            detail="slug must be alphanumeric + hyphens, 1-64 chars",
        )

    if await workspace_by_slug(session, body.slug) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"workspace with slug {body.slug!r} already exists",
        )
    # Release the pooled connection before the slow blueprint clone; a
    # concurrent create of the same slug is caught by UNIQUE(slug) below.
    await session.rollback()

    settings = request.app.state.settings
    fs_root: Path = settings.fs.root
    ws_uuid = uuid.uuid4()
    blueprint_ref = f"{body.blueprint}@v0.1"

    try:
        ws_dir = await asyncio.to_thread(
            clone_default_blueprint,
            fs_root,
            body.blueprint,
            ws_uuid,
            slug=body.slug,
            display_name=body.slug,
        )
    except (InvalidBlueprintNameError, UnknownBlueprintError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"unknown blueprint {body.blueprint!r}",
        ) from exc

    now = datetime.now(UTC)
    ws = Workspace(
        uuid=ws_uuid,
        slug=body.slug,
        display_name=body.slug,
        blueprint_ref=blueprint_ref,
        status="active",
        created_at=now,
        archived_at=None,
    )
    session.add(ws)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        try:
            await asyncio.to_thread(shutil.rmtree, ws_dir, ignore_errors=True)
        except Exception as cleanup_exc:
            logger.error(
                "failed to clean up orphaned workspace dir",
                path=str(ws_dir),
                exc=cleanup_exc,
            )
        raise HTTPException(
            status_code=409,
            detail=f"workspace with slug {body.slug!r} already exists",
        ) from exc

    return WorkspaceResponse(
        uuid=str(ws_uuid),
        slug=body.slug,
        blueprint_ref=blueprint_ref,
        created_at=now,
    )
