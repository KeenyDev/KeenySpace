from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"

    uuid: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(256))
    blueprint_ref: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    compile_state: Mapped[str] = mapped_column(String(32), server_default="idle")
    compile_paused_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compile_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_workspaces_status"),
        CheckConstraint(
            "compile_state IN ('idle', 'running', 'paused')", name="ck_workspaces_compile_state"
        ),
        CheckConstraint(
            "(status = 'archived') = (archived_at IS NOT NULL)",
            name="ck_workspaces_archived_at_matches_status",
        ),
        CheckConstraint(
            "compile_state <> 'paused' OR compile_paused_reason IS NOT NULL",
            name="ck_workspaces_paused_has_reason",
        ),
    )


class User(Base):
    __tablename__ = "users"

    sub: Mapped[str] = mapped_column(String(256), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    groups: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    groups_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_sub: Mapped[str] = mapped_column(String(256))
    token_hash: Mapped[str] = mapped_column(String(256))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_sub: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(16), default="ks_live_", server_default="ks_live_")
    hash: Mapped[str] = mapped_column(String(256))
    lookup_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    actor_sub: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(128))
    workspace_uuid: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Blueprint(Base):
    __tablename__ = "blueprints"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


COMPILE_RUN_STATUSES = (
    "running",
    "success",
    "idempotent_noop",
    "abort_budget",
    "abort_ceiling",
    "abort_space_budget",
    "abort_loop",
    "abort_llm_error",
    "abort_plan_invalid",
    "abort_error",
    "abort_interrupted",
)


class CompileCursor(Base):
    """Committed compile position plus at most one pending intent.

    last_wal_id is NULL until the first pass commits. A pending intent (plan written
    to disk, cursor not yet advanced) covers entries after last_wal_id up to and
    including pending_wal_last_id.
    """

    __tablename__ = "compile_cursors"

    workspace_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "workspaces.uuid", ondelete="CASCADE", name="compile_cursors_workspace_uuid_fkey"
        ),
        primary_key=True,
    )
    last_wal_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    last_compile_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pending_wal_last_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    pending_plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # JSON, not JSONB: JSONB reorders object keys and replayed frontmatter must keep
    # the agent-decided key order.
    pending_plan: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "(last_wal_id IS NULL) = (last_compile_hash IS NULL)",
            name="ck_compile_cursors_committed_pair",
        ),
        CheckConstraint(
            "(pending_wal_last_id IS NULL) = (pending_plan_hash IS NULL) "
            "AND (pending_plan_hash IS NULL) = (pending_plan IS NULL)",
            name="ck_compile_cursors_pending_complete",
        ),
    )


class CompileRun(Base):
    __tablename__ = "compile_runs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    workspace_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.uuid", ondelete="CASCADE"),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(64))
    trigger_source: Mapped[str] = mapped_column(String(32))
    wal_first_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    wal_last_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pages_written: Mapped[int] = mapped_column(default=0, server_default="0")
    tokens_input: Mapped[int] = mapped_column(default=0, server_default="0")
    tokens_output: Mapped[int] = mapped_column(default=0, server_default="0")
    duration_ms: Mapped[int | None]
    model: Mapped[str] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_compile_runs_workspace_started", "workspace_uuid", "started_at"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in COMPILE_RUN_STATUSES) + ")",
            name="ck_compile_runs_status",
        ),
    )
