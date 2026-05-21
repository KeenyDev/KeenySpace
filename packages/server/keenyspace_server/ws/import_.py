from __future__ import annotations

import asyncio
import os
import posixpath
import re
import secrets
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml
from keenyspace_shared.mcp_contracts import WorkspaceImportResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.auth.audit import write_audit
from keenyspace_server.db.models import Workspace
from keenyspace_server.fs.blueprint import _write_workspace_config
from keenyspace_server.observability.metrics import WORKSPACE_IMPORT_TOTAL

log = structlog.get_logger(__name__)

MAX_IMPORT_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

_SLUG_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$"
)


class WorkspaceImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WorkspaceSlugConflictError(ValueError):
    def __init__(self, slug: str) -> None:
        super().__init__(f"workspace with slug {slug!r} already exists")
        self.slug = slug


@dataclass(frozen=True)
class _ZipValidation:
    entries: list[zipfile.ZipInfo]
    total_bytes: int
    preserved_blueprint_ref: str | None


def _validate_zip_sync(zip_path: Path) -> _ZipValidation:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise WorkspaceImportError("bad_zip", f"zip is corrupt: {exc}") from exc

    try:
        infolist = zf.infolist()
        total = 0
        has_md = False
        for info in infolist:
            name = info.filename
            norm = posixpath.normpath(name)
            parts = norm.split("/")
            if ".." in parts or posixpath.isabs(norm):
                raise WorkspaceImportError(
                    "path_traversal",
                    f"unsafe zip entry: {name!r}",
                )
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise WorkspaceImportError(
                    "symlink",
                    f"zip entry is a symlink: {name!r}",
                )
            total += info.file_size
            if total > MAX_IMPORT_UNCOMPRESSED_BYTES:
                raise WorkspaceImportError(
                    "size_cap",
                    f"uncompressed size exceeds cap "
                    f"({MAX_IMPORT_UNCOMPRESSED_BYTES} bytes)",
                )
            if not info.is_dir() and name.endswith(".md"):
                has_md = True

        if not has_md:
            raise WorkspaceImportError(
                "empty_workspace",
                "zip contains no .md files",
            )

        preserved: str | None = None
        for info in infolist:
            if posixpath.normpath(info.filename) == ".keenyspace/config.yaml":
                try:
                    raw = zf.read(info).decode("utf-8", errors="replace")
                    data = yaml.safe_load(raw)
                    if isinstance(data, dict):
                        bp = data.get("blueprint")
                        if isinstance(bp, str):
                            preserved = bp
                except Exception as exc:
                    log.warning(
                        "workspace.import.config_yaml_parse_failed",
                        error=str(exc),
                    )
                break

        return _ZipValidation(
            entries=infolist,
            total_bytes=total,
            preserved_blueprint_ref=preserved,
        )
    finally:
        zf.close()


async def validate_import_zip(zip_path: Path) -> _ZipValidation:
    return await asyncio.to_thread(_validate_zip_sync, zip_path)


def _unpack_zip_sync(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            zf.extract(info, dest)


async def import_workspace(
    session: AsyncSession,
    *,
    settings: Any,
    slug: str,
    zip_path: Path,
    actor_sub: str,
) -> WorkspaceImportResponse:
    if not _SLUG_RE.match(slug):
        WORKSPACE_IMPORT_TOTAL.labels(outcome="validation_error").inc()
        raise WorkspaceImportError(
            "invalid_slug",
            "slug must be alphanumeric + hyphens, 1-64 chars",
        )

    existing = await session.execute(
        select(Workspace).where(Workspace.slug == slug)
    )
    if existing.scalar_one_or_none() is not None:
        WORKSPACE_IMPORT_TOTAL.labels(outcome="conflict").inc()
        raise WorkspaceSlugConflictError(slug)

    try:
        validation = await validate_import_zip(zip_path)
    except WorkspaceImportError:
        WORKSPACE_IMPORT_TOTAL.labels(outcome="validation_error").inc()
        raise

    new_uuid = uuid.uuid4()
    fs_root: Path = settings.fs.root
    workspaces_dir = fs_root / "workspaces"
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    # Stage extraction under a sibling .tmp/ tree so workspace iteration (admin
    # UI, doctor sweep) never sees ephemeral .import_tmp_* entries. The .tmp/
    # dir lives on the same fs_root mount as workspaces/, so the final
    # os.rename(import_tmp, final_dir) stays atomic.
    tmp_root = fs_root / ".tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    import_tmp = tmp_root / f"import_{secrets.token_hex(8)}"
    final_dir = workspaces_dir / str(new_uuid)

    cleanup_tmp = True
    outcome = "validation_error"
    try:
        import_tmp.mkdir(parents=True, exist_ok=False)
        await asyncio.to_thread(_unpack_zip_sync, zip_path, import_tmp)

        blueprint_ref = (
            validation.preserved_blueprint_ref
            if validation.preserved_blueprint_ref
            else "default@v0.1"
        )

        await asyncio.to_thread(
            _write_workspace_config,
            import_tmp,
            new_uuid,
            slug,
            slug,
            blueprint_ref,
        )

        now = datetime.now(UTC)
        ws = Workspace(
            uuid=new_uuid,
            slug=slug,
            display_name=slug,
            blueprint_ref=blueprint_ref,
            status="active",
            created_at=now,
            archived_at=None,
            compile_state="idle",
            compile_paused_reason=None,
            compile_paused_at=None,
        )
        session.add(ws)
        await write_audit(
            session,
            actor_sub=actor_sub,
            action="workspace.imported",
            workspace_uuid=new_uuid,
            payload={
                "workspace_slug": slug,
                "user_sub": actor_sub,
                "uncompressed_bytes": validation.total_bytes,
                "uuid": str(new_uuid),
                "blueprint_ref": blueprint_ref,
            },
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            outcome = "conflict"
            raise WorkspaceSlugConflictError(slug) from exc

        os.rename(import_tmp, final_dir)
        cleanup_tmp = False
        outcome = "success"
        log.info(
            "workspace.imported",
            workspace=str(new_uuid),
            slug=slug,
            uncompressed_bytes=validation.total_bytes,
        )
        return WorkspaceImportResponse(uuid=str(new_uuid), slug=slug)
    finally:
        if cleanup_tmp:
            shutil.rmtree(import_tmp, ignore_errors=True)
        WORKSPACE_IMPORT_TOTAL.labels(outcome=outcome).inc()
