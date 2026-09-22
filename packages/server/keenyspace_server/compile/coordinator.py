from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import structlog
from keenyspace_shared.loop_detector import LoopDetector
from pydantic_ai.exceptions import (
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from keenyspace_server.compile.agent import run_compile_agent
from keenyspace_server.compile.hashing import hash_plan
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
from keenyspace_server.observability.metrics import (
    COMPILE_DAILY_TOKENS,
    COMPILE_PAGES_WRITTEN_TOTAL,
    COMPILE_PASS_DURATION,
    COMPILE_PAUSED_TOTAL,
    COMPILE_RUNS_TOTAL,
)

# COMPILE_TOKENS_TOTAL increments deferred to v1.1 — real token accounting needs result.usage() wiring

log = structlog.get_logger(__name__)

_PG_UNDEFINED_TABLE = "42P01"


class CompileCursorConflictError(RuntimeError):
    """The compile cursor moved underneath a pass; its pages may not match the cursor."""


@dataclass
class _PassProgress:
    tokens_spent: bool = False


class CompileCoordinator:
    def __init__(self, settings: CompileSettings) -> None:
        self.settings = settings
        self._locks: dict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._dirty: set[UUID] = set()
        self._inflight: dict[UUID, str] = {}
        self._daily_tokens: dict[UUID, int] = defaultdict(int)
        self._output_tokens_per_space: dict[UUID, int] = defaultdict(int)
        self._pending_debounce: dict[UUID, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def notify_dirty(self, ws_uuid: UUID) -> None:
        """Sync, fire-and-forget. Schedules a debounced compile after settings.debounce_seconds.

        Idempotent within the debounce window: a second call for the same workspace
        cancels the pending timer and reschedules. If no event loop is running
        (e.g. called from sync test code), only the dirty-set is updated and the
        caller can drive trigger() manually.
        """
        self._dirty.add(ws_uuid)
        self._schedule(ws_uuid, self.settings.debounce_seconds, source="append")

    def _schedule(self, ws_uuid: UUID, delay: float, *, source: str) -> None:
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        pending = self._pending_debounce.pop(ws_uuid, None)
        if pending is not None:
            pending.cancel()

        def _fire() -> None:
            self._pending_debounce.pop(ws_uuid, None)
            self._spawn(self._trigger_after_debounce(ws_uuid, source))

        self._pending_debounce[ws_uuid] = loop.call_later(delay, _fire)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> bool:
        # trigger() awaits the DB between its own closed-check and here; a task spawned
        # after aclose() snapshotted self._tasks would outlive the engine.
        if self._closed:
            coro.close()
            return False
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _trigger_after_debounce(self, ws_uuid: UUID, source: str) -> None:
        try:
            await self.trigger(ws_uuid, source=source)
        except Exception as exc:
            log.warning("compile.debounce_trigger_failed", workspace=str(ws_uuid), error=str(exc))

    async def trigger(self, ws_uuid: UUID, source: str) -> CompileTriggerResponse:
        if self._closed:
            raise ValueError("compile coordinator is shutting down")
        ws_root = await self._workspace_root(ws_uuid)
        if ws_root is None:
            raise ValueError(
                f"workspace {ws_uuid}: filesystem directory does not exist; "
                "ensure workspace was created correctly before triggering compile"
            )

        ws_state = await self._workspace_state(ws_uuid)
        if ws_state == "paused":
            return CompileTriggerResponse(job_id=str(uuid4()), status="paused")

        lock = self._locks[ws_uuid]
        if lock.locked():
            in_flight = self._inflight.get(ws_uuid)
            if in_flight is not None:
                self._dirty.add(ws_uuid)
                return CompileTriggerResponse(job_id=in_flight, status="running")

        run_id = str(uuid4())
        if not self._spawn(self._run_locked_pass(ws_uuid, ws_root, run_id, source)):
            raise ValueError("compile coordinator is shutting down")
        return CompileTriggerResponse(job_id=run_id, status="queued")

    async def _run_locked_pass(
        self, ws_uuid: UUID, ws_root: Path, run_id: str, source: str
    ) -> None:
        result: CompileRunResult | None = None
        async with self._locks[ws_uuid]:
            self._inflight[ws_uuid] = run_id
            # Cleared before the slice is read: anything marked dirty from here on
            # may be missing from this pass and must earn a follow-up.
            self._dirty.discard(ws_uuid)
            try:
                result = await self._run_compile_pass(ws_uuid, ws_root, run_id, source)
            except Exception:
                log.error("compile.pass_failed", workspace=str(ws_uuid), run_id=run_id, exc_info=True)
            finally:
                self._inflight.pop(ws_uuid, None)
        if result is not None and result.backlog_remaining:
            self._schedule(ws_uuid, 0, source="backlog")
        elif ws_uuid in self._dirty:
            self._schedule(ws_uuid, self.settings.debounce_seconds, source="append")

    async def wait_for_idle(self, ws_uuid: UUID, *, timeout: float = 10.0) -> None:
        """Block until no compile pass is in-flight for `ws_uuid`. For test use."""
        deadline = asyncio.get_running_loop().time() + timeout
        while ws_uuid in self._inflight:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"compile pass for {ws_uuid} did not finish within {timeout}s")
            await asyncio.sleep(min(0.05, remaining))

    async def aclose(self) -> None:
        """Cancel pending debounce timers and in-flight passes; wait for them to finalize.

        Must run before the DB engine is disposed: cancelled passes write their
        terminal compile_runs status on the way out.
        """
        self._closed = True
        for handle in self._pending_debounce.values():
            handle.cancel()
        self._pending_debounce.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reconcile_interrupted(self) -> None:
        """Startup recovery: nothing can be running before the scheduler starts (single worker).

        Skipped with a warning when the schema is not migrated yet (auto-migrate off).
        """
        try:
            async with get_db_session() as session:
                runs = (await session.execute(
                    update(CompileRun)
                    .where(CompileRun.status == "running")
                    .values(status="abort_interrupted", completed_at=datetime.now(UTC))
                    .returning(CompileRun.id)
                )).scalars().all()
                workspaces = (await session.execute(
                    update(Workspace)
                    .where(Workspace.compile_state == "running")
                    .values(compile_state="idle")
                    .returning(Workspace.uuid)
                )).scalars().all()
                await session.commit()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != _PG_UNDEFINED_TABLE:
                raise
            log.warning("compile.reconcile_skipped", reason="schema_not_migrated")
            return
        if runs or workspaces:
            log.warning(
                "compile.reconciled_interrupted",
                runs=len(runs), workspaces=len(workspaces),
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
            # SPECIFICITY GUARD: WHERE clause MUST stay restricted to the daily-budget pause
            # reasons ('daily_ceiling', 'space_budget_exceeded') on active workspaces. NEVER
            # broaden to include 'archived' — that would erroneously resume archived workspaces
            # on the 00:00 UTC cron tick (Phase 4 D-01, RESEARCH.md Pitfall 6).
            await session.execute(
                update(Workspace)
                .where(
                    Workspace.status == "active",
                    Workspace.compile_paused_reason.in_(["daily_ceiling", "space_budget_exceeded"]),
                )
                .values(
                    compile_state="idle",
                    compile_paused_reason=None,
                    compile_paused_at=None,
                )
            )
            await session.commit()
        for ws in list(self._daily_tokens.keys()):
            COMPILE_DAILY_TOKENS.labels(workspace=str(ws)).set(0)
        self._daily_tokens.clear()
        self._output_tokens_per_space.clear()
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
        _pass_start = time.perf_counter()
        try:
            return await self._run_compile_pass_inner(ws_uuid, ws_root, run_id, source)
        finally:
            COMPILE_PASS_DURATION.labels(workspace=str(ws_uuid)).observe(
                time.perf_counter() - _pass_start
            )

    async def _run_compile_pass_inner(
        self,
        ws_uuid: UUID,
        ws_root: Path,
        run_id: str,
        source: str,
    ) -> CompileRunResult:
        # Compile activity is logged to compile_runs only; audit_log is reserved for
        # security events (per CONTEXT D-16). Do NOT add audit_log entries here.
        started_at = datetime.now(UTC)
        if not await self._claim_running(ws_uuid):
            log.info("compile.skipped", workspace=str(ws_uuid), run_id=run_id, reason="paused")
            return CompileRunResult(status="paused", pages_written=0)

        log.info("compile.started", workspace=str(ws_uuid), run_id=run_id, trigger_source=source)
        progress = _PassProgress()
        try:
            return await self._execute_pass(ws_uuid, ws_root, run_id, source, started_at, progress)
        except BaseException as exc:
            await self._finalize_failed_pass(ws_uuid, run_id, exc, tokens_spent=progress.tokens_spent)
            raise

    async def _execute_pass(
        self,
        ws_uuid: UUID,
        ws_root: Path,
        run_id: str,
        source: str,
        started_at: datetime,
        progress: _PassProgress,
    ) -> CompileRunResult:
        cursor_row = await self._read_cursor(ws_uuid)
        last_wal_id = cursor_row.last_wal_id if cursor_row else None
        slice_ = await asyncio.to_thread(
            extract_wal_slice, ws_root, last_wal_id, max_bytes=self.settings.max_slice_bytes
        )

        if not slice_.entries:
            await self._release_running(ws_uuid)
            log.info("compile.idempotent_noop", workspace=str(ws_uuid), run_id=run_id, reason="empty_slice")
            COMPILE_RUNS_TOTAL.labels(workspace=str(ws_uuid), status="idempotent_noop").inc()
            return CompileRunResult(status="idempotent_noop", pages_written=0)

        budget_abort: tuple[str, str, str] | None = None
        if self._daily_tokens.get(ws_uuid, 0) >= self.settings.daily_token_ceiling:
            budget_abort = ("abort_ceiling", "daily_ceiling", "daily token ceiling reached")
        elif self._output_tokens_per_space.get(ws_uuid, 0) >= self.settings.max_output_tokens_per_space:
            budget_abort = (
                "abort_space_budget", "space_budget_exceeded",
                "per-space daily output token budget reached",
            )
        if budget_abort is not None:
            status, reason, error = budget_abort
            await self._pause(ws_uuid, reason=reason, error=error)
            await self._write_run_row(
                ws_uuid, run_id, started_at, source,
                status=status, pages_written=0,
                wal_first_id=slice_.wal_first_id, wal_last_id=slice_.wal_last_id, plan_hash=None,
                completed_at=datetime.now(UTC),
            )
            log.warning("compile.aborted", workspace=str(ws_uuid), reason=reason)
            COMPILE_RUNS_TOTAL.labels(workspace=str(ws_uuid), status=status).inc()
            return CompileRunResult(status="paused", pages_written=0)

        await self._write_run_row(
            ws_uuid, run_id, started_at, source,
            status="running", pages_written=0,
            wal_first_id=slice_.wal_first_id, wal_last_id=slice_.wal_last_id, plan_hash=None,
        )

        deps = CompileDeps(ws_root=ws_root, wal_text=slice_.formatted_text)
        detector = LoopDetector(max_repeats=3)
        try:
            plan, detector, output_tokens = await asyncio.wait_for(
                run_compile_agent(
                    deps,
                    model_name=self.settings.model,
                    provider=self.settings.provider,
                    max_tool_calls=self.settings.max_tool_calls,
                    max_input_tokens=self.settings.max_input_tokens,
                    max_output_tokens_per_call=self.settings.max_output_tokens_per_call,
                    loop_detector=detector,
                ),
                timeout=self.settings.max_seconds,
            )
        except UsageLimitExceeded as exc:
            if detector.triggered:
                return await self._abort(
                    ws_uuid, run_id, status="abort_loop", reason="loop_abort", error=str(exc)
                )
            return await self._abort(
                ws_uuid, run_id, status="abort_budget", reason="budget_exceeded", error=str(exc)
            )
        except TimeoutError:
            return await self._abort(
                ws_uuid, run_id, status="abort_budget", reason="budget_exceeded", error="agent timeout"
            )
        except (ModelAPIError, UnexpectedModelBehavior) as exc:
            return await self._abort(
                ws_uuid, run_id, status="abort_llm_error", reason="llm_error", error=str(exc)
            )

        progress.tokens_spent = True
        # Tokens are spent once the agent returns, whether or not the plan lands on disk.
        # Per-space daily OUTPUT-token budget comes from real result.usage(); the daily
        # ceiling is a conservative estimate until v1.1. Both reset at 00:00 UTC.
        self._output_tokens_per_space[ws_uuid] = (
            self._output_tokens_per_space.get(ws_uuid, 0) + output_tokens
        )
        estimated_tokens = max(1, len(deps.wal_text) // 4)
        self._daily_tokens[ws_uuid] = self._daily_tokens.get(ws_uuid, 0) + estimated_tokens
        COMPILE_DAILY_TOKENS.labels(workspace=str(ws_uuid)).set(self._daily_tokens[ws_uuid])

        plan_hash_value = hash_plan(slice_.wal_first_id or "", slice_.wal_last_id or "", plan)

        if cursor_row is not None and plan_hash_value == cursor_row.last_compile_hash:
            await self._update_run_row(
                ws_uuid, run_id, status="idempotent_noop",
                plan_hash=plan_hash_value, completed_at=datetime.now(UTC),
            )
            await self._release_running(ws_uuid)
            log.info("compile.idempotent_noop", workspace=str(ws_uuid), run_id=run_id, reason="hash_match")
            COMPILE_RUNS_TOTAL.labels(workspace=str(ws_uuid), status="idempotent_noop").inc()
            return CompileRunResult(status="idempotent_noop", pages_written=0, plan_hash=plan_hash_value)

        try:
            pages_written = await asyncio.to_thread(apply_plan, ws_root, plan)
        except CompilePlanSafetyError as exc:
            return await self._abort(
                ws_uuid, run_id, status="abort_plan_invalid", reason="plan_invalid",
                error=str(exc), plan_hash=plan_hash_value,
            )

        await self._advance_cursor(ws_uuid, slice_.wal_last_id or "", plan_hash_value, last_wal_id)

        for op in plan.ops:
            COMPILE_PAGES_WRITTEN_TOTAL.labels(workspace=str(ws_uuid), action=op.action).inc()
        COMPILE_RUNS_TOTAL.labels(workspace=str(ws_uuid), status="success").inc()

        await self._update_run_row(
            ws_uuid, run_id,
            status="success",
            pages_written=pages_written,
            plan_hash=plan_hash_value,
            tokens_output=output_tokens,
            completed_at=datetime.now(UTC),
        )
        # This run succeeded; pause future runs if it pushed the space over its daily budget.
        if self._output_tokens_per_space[ws_uuid] >= self.settings.max_output_tokens_per_space:
            await self._pause(
                ws_uuid, reason="space_budget_exceeded",
                error="per-space daily output token budget reached",
            )
        else:
            await self._release_running(ws_uuid)
        log.info(
            "compile.finished",
            workspace=str(ws_uuid), run_id=run_id,
            pages_written=pages_written, plan_hash=plan_hash_value,
            backlog_remaining=slice_.has_more,
        )
        return CompileRunResult(
            status="success",
            pages_written=pages_written,
            plan_hash=plan_hash_value,
            backlog_remaining=slice_.has_more,
        )

    async def _abort(
        self, ws_uuid: UUID, run_id: str,
        *, status: str, reason: str, error: str, plan_hash: str | None = None,
    ) -> CompileRunResult:
        await self._pause(ws_uuid, reason=reason, error=error)
        await self._update_run_row(
            ws_uuid, run_id, status=status, error_message=error,
            plan_hash=plan_hash, completed_at=datetime.now(UTC),
        )
        COMPILE_RUNS_TOTAL.labels(workspace=str(ws_uuid), status=status).inc()
        log.warning("compile.aborted", workspace=str(ws_uuid), run_id=run_id, reason=reason)
        return CompileRunResult(status="paused", pages_written=0, plan_hash=plan_hash)

    async def _finalize_failed_pass(
        self, ws_uuid: UUID, run_id: str, exc: BaseException, *, tokens_spent: bool
    ) -> None:
        interrupted = not isinstance(exc, Exception)
        status = "abort_interrupted" if interrupted else "abort_error"
        error = "compile pass interrupted" if interrupted else f"{type(exc).__name__}: {exc}"
        try:
            await self._update_run_row(
                ws_uuid, run_id, status=status, error_message=error,
                completed_at=datetime.now(UTC), only_if_running=True,
            )
            if tokens_spent and not interrupted:
                # Pausing (not idling) stops the backstop from re-spending LLM tokens
                # every 15 minutes on a failure that will most likely repeat. Failures
                # before the agent ran cost nothing to retry, so they go back to idle.
                await self._pause(ws_uuid, reason="internal_error", error=error)
            else:
                await self._release_running(ws_uuid)
        except Exception as finalize_exc:
            log.error(
                "compile.finalize_failed",
                workspace=str(ws_uuid), run_id=run_id,
                error=str(finalize_exc), original_error=error,
            )
        COMPILE_RUNS_TOTAL.labels(workspace=str(ws_uuid), status=status).inc()
        if interrupted:
            log.warning("compile.interrupted", workspace=str(ws_uuid), run_id=run_id)

    async def resume(self, ws_uuid: UUID) -> None:
        """Manual reset of a paused, active workspace. Idempotent. Per D-14.

        Archived workspaces stay paused (unarchive is the only way out), and the
        per-space daily token tally is kept so resume cannot bypass the budget.
        """
        async with get_db_session() as session:
            await session.execute(
                update(Workspace)
                .where(
                    Workspace.uuid == ws_uuid,
                    Workspace.status == "active",
                    Workspace.compile_state == "paused",
                )
                .values(
                    compile_state="idle",
                    compile_paused_reason=None,
                    compile_paused_at=None,
                )
            )
            await session.commit()
        log.info("compile.resumed", workspace=str(ws_uuid))

    async def _claim_running(self, ws_uuid: UUID) -> bool:
        async with get_db_session() as session:
            claimed = (await session.execute(
                update(Workspace)
                .where(Workspace.uuid == ws_uuid, Workspace.compile_state != "paused")
                .values(compile_state="running")
                .returning(Workspace.uuid)
            )).scalar_one_or_none()
            await session.commit()
        return claimed is not None

    async def _release_running(self, ws_uuid: UUID) -> None:
        async with get_db_session() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.uuid == ws_uuid, Workspace.compile_state == "running")
                .values(compile_state="idle")
            )
            await session.commit()

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
                    raise CompileCursorConflictError(
                        f"compile cursor for {ws_uuid} moved from {expected_last_wal_id!r} during the pass"
                    )
            await session.commit()

    async def _write_run_row(
        self, ws_uuid: UUID, run_id: str, started_at: datetime, source: str,
        *, status: str, pages_written: int,
        wal_first_id: str | None, wal_last_id: str | None, plan_hash: str | None,
        completed_at: datetime | None = None,
    ) -> None:
        async with get_db_session() as session:
            session.add(CompileRun(
                id=UUID(run_id), workspace_uuid=ws_uuid,
                started_at=started_at, completed_at=completed_at,
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
        error_message: str | None = None, tokens_output: int | None = None,
        only_if_running: bool = False,
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
            if tokens_output is not None:
                values["tokens_output"] = tokens_output
            if values:
                stmt = update(CompileRun).where(CompileRun.id == UUID(run_id))
                if only_if_running:
                    stmt = stmt.where(CompileRun.status == "running")
                await session.execute(stmt.values(**values))
                await session.commit()

    async def _pause(self, ws_uuid: UUID, *, reason: str, error: str) -> None:
        # Only the pass that owns 'running' may pause: an archive (or any other pause)
        # that landed mid-pass keeps its reason instead of being overwritten by a
        # budget reason the 00:00 UTC cron would later clear.
        async with get_db_session() as session:
            paused = (await session.execute(
                update(Workspace)
                .where(Workspace.uuid == ws_uuid, Workspace.compile_state == "running")
                .values(
                    compile_state="paused",
                    compile_paused_reason=reason,
                    compile_paused_at=datetime.now(UTC),
                )
                .returning(Workspace.uuid)
            )).scalar_one_or_none()
            await session.commit()
        if paused is None:
            log.info("compile.pause_skipped", workspace=str(ws_uuid), reason=reason, error=error)
            return
        COMPILE_PAUSED_TOTAL.labels(workspace=str(ws_uuid), reason=reason).inc()
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
