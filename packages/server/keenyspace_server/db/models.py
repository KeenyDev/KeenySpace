from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String
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
    created_at: Mapped[datetime]
    archived_at: Mapped[datetime | None]
    compile_state: Mapped[str] = mapped_column(String(32), server_default="idle")
    compile_paused_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compile_paused_at: Mapped[datetime | None]

    __table_args__ = (Index("ix_workspaces_slug", "slug"),)


class User(Base):
    __tablename__ = "users"

    sub: Mapped[str] = mapped_column(String(256), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime]


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_sub: Mapped[str] = mapped_column(String(256))
    token_hash: Mapped[str] = mapped_column(String(256))
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime]


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_sub: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(16), default="ks_live_")
    hash: Mapped[str] = mapped_column(String(256))
    lookup_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    created_at: Mapped[datetime]


class CompileCursor(Base):
    __tablename__ = "compile_cursors"

    workspace_uuid: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    last_wal_id: Mapped[str] = mapped_column(String(26))
    last_compile_hash: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime]


class CompileRun(Base):
    __tablename__ = "compile_runs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    workspace_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.uuid", ondelete="CASCADE"),
    )
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(64))
    trigger_source: Mapped[str] = mapped_column(String(32))
    wal_first_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    wal_last_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pages_written: Mapped[int] = mapped_column(default=0)
    tokens_input: Mapped[int] = mapped_column(default=0)
    tokens_output: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int | None]
    model: Mapped[str] = mapped_column(String(128))
    error_message: Mapped[str | None]

    __table_args__ = (Index("ix_compile_runs_workspace_started", "workspace_uuid", "started_at"),)
