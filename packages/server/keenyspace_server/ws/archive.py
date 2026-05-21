from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
import yaml
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.auth.audit import write_audit
from keenyspace_server.db.models import Workspace
from keenyspace_server.fs.atomic import write_atomic
from keenyspace_server.observability.metrics import WORKSPACE_ARCHIVE_TOTAL

log = structlog.get_logger(__name__)


class ArchiveConflictError(ValueError):
    pass


async def archive_workspace(
    session: AsyncSession,
    *,
    ws_uuid: UUID,
    ws_dir: Path,
    actor_sub: str,
    slug: str,
) -> datetime:
    now = datetime.now(UTC)
    result = await session.execute(
        update(Workspace)
        .where(Workspace.uuid == ws_uuid, Workspace.status == "active")
        .values(
            status="archived",
            archived_at=now,
            compile_state="paused",
            compile_paused_reason="archived",
            compile_paused_at=now,
        )
        .returning(Workspace.uuid)
    )
    if result.scalar_one_or_none() is None:
        raise ArchiveConflictError(
            f"workspace {slug!r} not found or already archived"
        )
    await write_audit(
        session,
        actor_sub=actor_sub,
        action="workspace.archived",
        workspace_uuid=ws_uuid,
        payload={"slug": slug, "trigger": "user"},
    )
    await session.commit()

    # D-03 step 3: best-effort .keenyspace/config.yaml mirror; DB is source of
    # truth. Failures are logged and swallowed; `keenyspace doctor` (Phase 5)
    # reconciles drift.
    _mirror_archived_at_to_config(ws_dir, now)

    WORKSPACE_ARCHIVE_TOTAL.labels(action="archive").inc()
    log.info("workspace.archived", workspace=str(ws_uuid), slug=slug)
    return now


async def unarchive_workspace(
    session: AsyncSession,
    *,
    ws_uuid: UUID,
    ws_dir: Path,
    actor_sub: str,
    slug: str,
) -> None:
    status_result = await session.execute(
        update(Workspace)
        .where(Workspace.uuid == ws_uuid, Workspace.status == "archived")
        .values(status="active", archived_at=None)
        .returning(Workspace.uuid)
    )
    if status_result.scalar_one_or_none() is None:
        raise ArchiveConflictError(
            f"workspace {slug!r} not found or not archived"
        )

    # Selective compile-state reset: only clear pause if reason was 'archived'.
    # Other pause reasons (daily_ceiling, loop_abort, ...) survive unarchive and
    # require explicit POST /compile/resume to clear (D-01 unarchive semantics).
    await session.execute(
        update(Workspace)
        .where(
            Workspace.uuid == ws_uuid,
            Workspace.compile_paused_reason == "archived",
        )
        .values(
            compile_state="idle",
            compile_paused_reason=None,
            compile_paused_at=None,
        )
    )
    await write_audit(
        session,
        actor_sub=actor_sub,
        action="workspace.unarchived",
        workspace_uuid=ws_uuid,
        payload={"slug": slug, "trigger": "user"},
    )
    await session.commit()

    _mirror_archived_at_to_config(ws_dir, archived_at=None)

    WORKSPACE_ARCHIVE_TOTAL.labels(action="unarchive").inc()
    log.info("workspace.unarchived", workspace=str(ws_uuid), slug=slug)


def _mirror_archived_at_to_config(ws_dir: Path, archived_at: datetime | None) -> None:
    config_path = ws_dir / ".keenyspace" / "config.yaml"
    try:
        if not config_path.exists():
            log.warning(
                "workspace.config_yaml_missing", path=str(config_path)
            )
            return
        with contextlib.suppress(Exception):
            existing_text = config_path.read_text()
            data: dict[str, Any]
            loaded = yaml.safe_load(existing_text)
            data = loaded if isinstance(loaded, dict) else {}
            if archived_at is not None:
                data["archived_at"] = archived_at.isoformat()
            else:
                data.pop("archived_at", None)
            write_atomic(
                config_path,
                yaml.dump(data, allow_unicode=True, sort_keys=False).encode(),
            )
    except Exception as exc:
        log.warning(
            "workspace.config_yaml_mirror_failed",
            path=str(config_path),
            error=str(exc),
        )
