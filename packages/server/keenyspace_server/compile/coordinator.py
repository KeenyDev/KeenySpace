from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import structlog
from pydantic_ai.exceptions import UsageLimitExceeded
from sqlalchemy import select, update

from keenyspace_server.compile.agent import run_compile_agent
from keenyspace_server.compile.hashing import hash_plan
from keenyspace_server.compile.loop_detector import LoopDetector
from keenyspace_server.compile.models import (
    CompileDeps,
    CompileRunResult,
    CompileStatusResponse,
    CompileTriggerResponse,
)
from keenyspace_server.compile.page_writer import CompilePlanSafetyError, apply_plan
from keenyspace_server.compile.settings import CompileSettings
from keenyspace_server.compile.wal_slice import extract_wal_slice
from keenyspace_server.db.models import CompileCursor, CompileRun, Workspace
from keenyspace_server.db.session import get_db_session

log = structlog.get_logger(__name__)


class CompileCoordinator:
    def __init__(self, settings: CompileSettings) -> None:
        self.settings = settings
        self._locks: dict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._dirty: set[UUID] = set()
        self._inflight: dict[UUID, str] = {}
        self._daily_tokens: dict[UUID, int] = defaultdict(int)

    def notify_dirty(self, ws_uuid: UUID) -> None:
        """Sync, fire-and-forget. Plan 03 stub: only records the dirty signal.

        Plan 05 replaces this with debounce-and-reschedule wiring; the full CMP-01
        lifecycle (idle→debounced→running→idle) is therefore verified only after Plan 05.
        """
        self._dirty.add(ws_uuid)

    async def trigger(self, ws_uuid: UUID, source: str) -> CompileTriggerResponse:
        ws_root = await self._workspace_root(ws_uuid)
        if ws_root is None:
            return CompileTriggerResponse(job_id=str(uuid4()), status="paused")

        ws_state = await self._workspace_state(ws_uuid)
        if ws_state == "paused":
            return CompileTriggerResponse(job_id=str(uuid4()), status="paused")

        lock = self._locks[ws_uuid]
        if lock.locked():
            in_flight = self._inflight.get(ws_uuid)
            if in_flight is not None:
                return CompileTriggerResponse(job_id=in_flight, status="running")

        async with lock:
            run_id = str(uuid4())
            self._inflight[ws_uuid] = run_id
            try:
                result = await self._run_compile_pass(ws_uuid, ws_root, run_id, source)
            finally:
                self._inflight.pop(ws_uuid, None)
                self._dirty.discard(ws_uuid)
        return CompileTriggerResponse(
            job_id=run_id,
            status="idempotent_noop" if result.status == "idempotent_noop" else "queued",
        )

    async def status(self, ws_uuid: UUID) -> CompileStatusResponse:
        async with get_db_session() as session:
            ws_row = (await session.execute(
                select(Workspace).where(Workspace.uuid == ws_uuid)
            )).scalar_one_or_none()
            cur_row = (await session.execute(
                select(CompileCursor).where(CompileCursor.workspace_uuid == ws_uuid)
            )).scalar_one_or_none()
            last_run = (await session.execute(
                select(CompileRun)
                .where(CompileRun.workspace_uuid == ws_uuid)
                .order_by(CompileRun.started_at.desc())
                .limit(1)
            )).scalar_one_or_none()
        if ws_row is None:
            return CompileStatusResponse(state="idle")
        from typing import Literal, cast
        safe_state = ws_row.compile_state if ws_row.compile_state in ("idle", "running", "paused") else "idle"
        return CompileStatusResponse(
            state=cast(Literal["idle", "running", "paused"], safe_state),
            last_wal_id=cur_row.last_wal_id if cur_row else None,
            last_compile_at=last_run.completed_at if last_run else None,
            paused_reason=ws_row.compile_paused_reason,
            paused_at=ws_row.compile_paused_at,
        )

    async def backstop_all_workspaces(self) -> None:
        """APScheduler entry point (Plan 05). Triggers a compile pass against every active workspace."""
        async with get_db_session() as session:
            rows = (await session.execute(
                select(Workspace.uuid).where(Workspace.archived_at.is_(None))
            )).scalars().all()
        for ws_uuid in rows:
            try:
                await self.trigger(ws_uuid, source="backstop")
            except Exception as exc:
                log.warning("compile.backstop_failed", workspace=str(ws_uuid), error=str(exc))

    async def reset_daily_ceiling(self) -> None:
        """APScheduler 00:00 UTC cron entry point (Plan 05 + D-14)."""
        async with get_db_session() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.compile_paused_reason == "daily_ceiling")
                .values(
                    compile_state="idle",
                    compile_paused_reason=None,
                    compile_paused_at=None,
                )
            )
            await session.commit()
        self._daily_tokens.clear()
        log.info("compile.daily_ceiling_reset")

    async def _workspace_root(self, ws_uuid: UUID) -> Path | None:
        from keenyspace_server.config import get_settings
        settings = get_settings()
        root = Path(settings.fs.root) / "workspaces" / str(ws_uuid)
        return root if root.is_dir() else None

    async def _workspace_state(self, ws_uuid: UUID) -> str:
        async with get_db_session() as session:
            row = (await session.execute(
                select(Workspace.compile_state).where(Workspace.uuid == ws_uuid)
            )).scalar_one_or_none()
        return row or "idle"

    async def _run_compile_pass(
        self,
        ws_uuid: UUID,
        ws_root: Path,
        run_id: str,
        source: str,
    ) -> CompileRunResult:
        started_at = datetime.now(UTC)
        log.info("compile.started", workspace=str(ws_uuid), run_id=run_id, trigger_source=source)

        cursor_row = await self._read_cursor(ws_uuid)
        last_wal_id = cursor_row.last_wal_id if cursor_row else None
        slice_ = await asyncio.to_thread(extract_wal_slice, ws_root, last_wal_id)

        if not slice_.entries:
            await self._write_run_row(
                ws_uuid, run_id, started_at, source,
                status="idempotent_noop", pages_written=0,
                wal_first_id=None, wal_last_id=None, plan_hash=None,
            )
            log.info("compile.idempotent_noop", workspace=str(ws_uuid), run_id=run_id, reason="empty_slice")
            return CompileRunResult(status="idempotent_noop", pages_written=0)

        if self._daily_tokens.get(ws_uuid, 0) >= self.settings.daily_token_ceiling:
            await self._pause(ws_uuid, reason="daily_ceiling", error="daily token ceiling reached")
            await self._write_run_row(
                ws_uuid, run_id, started_at, source,
                status="abort_ceiling", pages_written=0,
                wal_first_id=slice_.wal_first_id, wal_last_id=slice_.wal_last_id, plan_hash=None,
            )
            log.warning("compile.aborted", workspace=str(ws_uuid), reason="daily_ceiling")
            return CompileRunResult(status="paused", pages_written=0)

        await self._write_run_row(
            ws_uuid, run_id, started_at, source,
            status="running", pages_written=0,
            wal_first_id=slice_.wal_first_id, wal_last_id=slice_.wal_last_id, plan_hash=None,
        )

        deps = CompileDeps(ws_root=ws_root, wal_text=slice_.formatted_text)
        detector = LoopDetector(max_repeats=3)
        try:
            plan, detector = await asyncio.wait_for(
                run_compile_agent(
                    deps,
                    model_name=self.settings.model,
                    max_tool_calls=self.settings.max_tool_calls,
                    max_output_tokens=self.settings.max_output_tokens,
                    loop_detector=detector,
                ),
                timeout=self.settings.max_seconds,
            )
        except UsageLimitExceeded as exc:
            if detector.triggered:
                await self._pause(ws_uuid, reason="loop_abort", error=str(exc))
                await self._update_run_row(ws_uuid, run_id, status="abort_loop", error_message=str(exc))
            else:
                await self._pause(ws_uuid, reason="budget_exceeded", error=str(exc))
                await self._update_run_row(ws_uuid, run_id, status="abort_budget", error_message=str(exc))
            raise
        except TimeoutError:
            await self._pause(ws_uuid, reason="timeout", error="agent timeout")
            await self._update_run_row(ws_uuid, run_id, status="abort_budget", error_message="timeout")
            raise

        plan_hash_value = hash_plan(slice_.wal_first_id or "", slice_.wal_last_id or "", plan)

        if cursor_row is not None and plan_hash_value == cursor_row.last_compile_hash:
            await self._update_run_row(
                ws_uuid, run_id, status="idempotent_noop",
                plan_hash=plan_hash_value, completed_at=datetime.now(UTC),
            )
            log.info("compile.idempotent_noop", workspace=str(ws_uuid), run_id=run_id, reason="hash_match")
            return CompileRunResult(status="idempotent_noop", pages_written=0, plan_hash=plan_hash_value)

        try:
            pages_written = await asyncio.to_thread(apply_plan, ws_root, plan)
        except CompilePlanSafetyError as exc:
            await self._pause(ws_uuid, reason="plan_invalid", error=f"denylist: {exc.path}")
            await self._update_run_row(
                ws_uuid, run_id, status="abort_plan_invalid",
                error_message=str(exc), plan_hash=plan_hash_value,
            )
            raise

        await self._advance_cursor(ws_uuid, slice_.wal_last_id or "", plan_hash_value, last_wal_id)

        # Conservative daily-token tracking; v1.1 will replace with result.usage() data
        estimated_tokens = max(1, len(deps.wal_text) // 4)
        self._daily_tokens[ws_uuid] = self._daily_tokens.get(ws_uuid, 0) + estimated_tokens

        await self._update_run_row(
            ws_uuid, run_id,
            status="success",
            pages_written=pages_written,
            plan_hash=plan_hash_value,
            completed_at=datetime.now(UTC),
        )
        log.info(
            "compile.finished",
            workspace=str(ws_uuid), run_id=run_id,
            pages_written=pages_written, plan_hash=plan_hash_value,
        )
        return CompileRunResult(status="success", pages_written=pages_written, plan_hash=plan_hash_value)

    async def resume(self, ws_uuid: UUID) -> None:
        """Manual reset of paused workspace state. Idempotent. Per D-14."""
        async with get_db_session() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.uuid == ws_uuid)
                .values(
                    compile_state="idle",
                    compile_paused_reason=None,
                    compile_paused_at=None,
                )
            )
            await session.commit()
        log.info("compile.resumed", workspace=str(ws_uuid))

    async def _read_cursor(self, ws_uuid: UUID) -> CompileCursor | None:
        async with get_db_session() as session:
            return (await session.execute(
                select(CompileCursor).where(CompileCursor.workspace_uuid == ws_uuid)
            )).scalar_one_or_none()

    async def _advance_cursor(
        self, ws_uuid: UUID, new_last_wal_id: str, plan_hash_value: str, expected_last_wal_id: str | None,
    ) -> None:
        async with get_db_session() as session:
            if expected_last_wal_id is None:
                session.add(CompileCursor(
                    workspace_uuid=ws_uuid,
                    last_wal_id=new_last_wal_id,
                    last_compile_hash=plan_hash_value,
                    updated_at=datetime.now(UTC),
                ))
            else:
                # CAS update; zero rows = concurrent restart, log + proceed (Plan 04 may add retry)
                res = await session.execute(
                    update(CompileCursor)
                    .where(
                        CompileCursor.workspace_uuid == ws_uuid,
                        CompileCursor.last_wal_id == expected_last_wal_id,
                    )
                    .values(
                        last_wal_id=new_last_wal_id,
                        last_compile_hash=plan_hash_value,
                        updated_at=datetime.now(UTC),
                    )
                    .returning(CompileCursor.workspace_uuid)
                )
                if res.scalar_one_or_none() is None:
                    log.warning("compile.cursor_cas_conflict", workspace=str(ws_uuid), expected=expected_last_wal_id)
            await session.commit()

    async def _write_run_row(
        self, ws_uuid: UUID, run_id: str, started_at: datetime, source: str,
        *, status: str, pages_written: int,
        wal_first_id: str | None, wal_last_id: str | None, plan_hash: str | None,
    ) -> None:
        async with get_db_session() as session:
            session.add(CompileRun(
                id=UUID(run_id), workspace_uuid=ws_uuid,
                started_at=started_at, completed_at=None,
                status=status, trigger_source=source,
                wal_first_id=wal_first_id, wal_last_id=wal_last_id, plan_hash=plan_hash,
                pages_written=pages_written,
                tokens_input=0, tokens_output=0, duration_ms=None,
                model=self.settings.model, error_message=None,
            ))
            await session.commit()

    async def _update_run_row(
        self, ws_uuid: UUID, run_id: str,
        *, status: str | None = None, pages_written: int | None = None,
        plan_hash: str | None = None, completed_at: datetime | None = None,
        error_message: str | None = None,
    ) -> None:
        async with get_db_session() as session:
            values: dict[str, object] = {}
            if status is not None:
                values["status"] = status
            if pages_written is not None:
                values["pages_written"] = pages_written
            if plan_hash is not None:
                values["plan_hash"] = plan_hash
            if completed_at is not None:
                values["completed_at"] = completed_at
            if error_message is not None:
                values["error_message"] = error_message
            if values:
                await session.execute(
                    update(CompileRun)
                    .where(CompileRun.id == UUID(run_id))
                    .values(**values)
                )
                await session.commit()

    async def _pause(self, ws_uuid: UUID, *, reason: str, error: str) -> None:
        async with get_db_session() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.uuid == ws_uuid)
                .values(
                    compile_state="paused",
                    compile_paused_reason=reason,
                    compile_paused_at=datetime.now(UTC),
                )
            )
            await session.commit()
        log.warning(
            "compile.paused",
            workspace=str(ws_uuid), reason=reason, error=error,
        )


_coordinator_singleton: CompileCoordinator | None = None


def get_coordinator() -> CompileCoordinator | None:
    return _coordinator_singleton


def set_coordinator(c: CompileCoordinator | None) -> None:
    global _coordinator_singleton
    _coordinator_singleton = c
