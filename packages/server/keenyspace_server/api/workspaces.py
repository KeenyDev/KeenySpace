from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db
from keenyspace_server.fs.blueprint import clone_default_blueprint

router = APIRouter()

_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


class WorkspaceCreateRequest(BaseModel):
    slug: str
    blueprint: str = "default"


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
    session: AsyncSession = Depends(get_db),
) -> WorkspaceResponse:
    if not _SLUG_RE.match(body.slug):
        raise HTTPException(
            status_code=422,
            detail="slug must be alphanumeric + hyphens, 1-64 chars",
        )

    existing = await session.execute(
        select(Workspace).where(Workspace.slug == body.slug)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"workspace with slug {body.slug!r} already exists",
        )

    settings = request.app.state.settings
    fs_root: Path = settings.fs.root
    ws_uuid = uuid.uuid4()
    blueprint_ref = f"{body.blueprint}@v0.1"

    ws_dir = clone_default_blueprint(
        fs_root,
        body.blueprint,
        ws_uuid,
        slug=body.slug,
        display_name=body.slug,
    )

    now = datetime.now(timezone.utc)
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
    except IntegrityError:
        await session.rollback()
        try:
            shutil.rmtree(ws_dir, ignore_errors=True)
        except Exception:
            pass
        raise HTTPException(
            status_code=409,
            detail=f"workspace with slug {body.slug!r} already exists",
        )

    return WorkspaceResponse(
        uuid=str(ws_uuid),
        slug=body.slug,
        blueprint_ref=blueprint_ref,
        created_at=now,
    )
