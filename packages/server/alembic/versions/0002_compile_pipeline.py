"""compile pipeline: compile_runs table + workspaces compile columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("compile_state", sa.String(32), nullable=False, server_default="idle"),
    )
    op.add_column("workspaces", sa.Column("compile_paused_reason", sa.String(64), nullable=True))
    op.add_column("workspaces", sa.Column("compile_paused_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "compile_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_uuid",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.uuid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("trigger_source", sa.String(32), nullable=False),
        sa.Column("wal_first_id", sa.String(26), nullable=True),
        sa.Column("wal_last_id", sa.String(26), nullable=True),
        sa.Column("plan_hash", sa.String(64), nullable=True),
        sa.Column("pages_written", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_input", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_compile_runs_workspace_started",
        "compile_runs",
        ["workspace_uuid", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_compile_runs_workspace_started", table_name="compile_runs")
    op.drop_table("compile_runs")
    op.drop_column("workspaces", "compile_paused_at")
    op.drop_column("workspaces", "compile_paused_reason")
    op.drop_column("workspaces", "compile_state")
