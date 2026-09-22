"""integrity constraints, compile_cursors FK + pending compile intent

Adds CHECK constraints for the workspace/compile state machines, the missing
compile_cursors -> workspaces foreign key, and the pending-intent columns the
coordinator records before writing pages so a crash between the page writes and
the cursor advance replays the stored plan instead of recompiling the slice.
compile_cursors.last_wal_id/last_compile_hash become nullable: a first-ever pass
has no committed position yet but must still record its intent.

Drops ix_workspaces_slug, which duplicated the UNIQUE constraint's index.

Existing rows are checked before any DDL; violations abort the migration with
counts instead of being silently rewritten.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

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


def _in_list(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


WORKSPACE_CHECKS = {
    "ck_workspaces_status": _in_list("status", ("active", "archived")),
    "ck_workspaces_compile_state": _in_list("compile_state", ("idle", "running", "paused")),
    "ck_workspaces_archived_at_matches_status": "(status = 'archived') = (archived_at IS NOT NULL)",
    "ck_workspaces_paused_has_reason": "compile_state <> 'paused' OR compile_paused_reason IS NOT NULL",
}
COMPILE_RUN_CHECKS = {
    "ck_compile_runs_status": _in_list("status", COMPILE_RUN_STATUSES),
}
COMPILE_CURSOR_CHECKS = {
    "ck_compile_cursors_committed_pair": "(last_wal_id IS NULL) = (last_compile_hash IS NULL)",
    "ck_compile_cursors_pending_complete": (
        "(pending_wal_last_id IS NULL) = (pending_plan_hash IS NULL) "
        "AND (pending_plan_hash IS NULL) = (pending_plan IS NULL)"
    ),
}
PREEXISTING_CHECKS = {
    "workspaces": WORKSPACE_CHECKS,
    "compile_runs": COMPILE_RUN_CHECKS,
    "compile_cursors": {"ck_compile_cursors_committed_pair": COMPILE_CURSOR_CHECKS["ck_compile_cursors_committed_pair"]},
}


def _preflight(bind: sa.engine.Connection) -> None:
    violations: list[str] = []
    for table, checks in PREEXISTING_CHECKS.items():
        for name, predicate in checks.items():
            count = bind.execute(
                sa.text(f"SELECT count(*) FROM {table} WHERE NOT ({predicate})")
            ).scalar_one()
            if count:
                violations.append(f"{table}: {count} row(s) violate {name} ({predicate})")
    orphans = bind.execute(sa.text(
        "SELECT count(*) FROM compile_cursors c "
        "WHERE NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.uuid = c.workspace_uuid)"
    )).scalar_one()
    if orphans:
        violations.append(
            f"compile_cursors: {orphans} row(s) reference a missing workspace "
            "(compile_cursors_workspace_uuid_fkey)"
        )
    if violations:
        raise RuntimeError(
            "0005 refuses to run until existing rows are repaired manually:\n  "
            + "\n  ".join(violations)
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    _preflight(bind)

    op.drop_index("ix_workspaces_slug", table_name="workspaces")
    for name, predicate in WORKSPACE_CHECKS.items():
        op.create_check_constraint(name, "workspaces", predicate)
    for name, predicate in COMPILE_RUN_CHECKS.items():
        op.create_check_constraint(name, "compile_runs", predicate)

    op.create_foreign_key(
        "compile_cursors_workspace_uuid_fkey",
        "compile_cursors", "workspaces",
        ["workspace_uuid"], ["uuid"],
        ondelete="CASCADE",
    )
    op.alter_column("compile_cursors", "last_wal_id", existing_type=sa.String(26), nullable=True)
    op.alter_column("compile_cursors", "last_compile_hash", existing_type=sa.String(64), nullable=True)
    op.add_column("compile_cursors", sa.Column("pending_wal_last_id", sa.String(26), nullable=True))
    op.add_column("compile_cursors", sa.Column("pending_plan_hash", sa.String(64), nullable=True))
    # JSON, not JSONB: JSONB reorders object keys, and a replayed plan must write the
    # frontmatter in the agent-decided order the original apply would have used.
    op.add_column("compile_cursors", sa.Column("pending_plan", sa.JSON(), nullable=True))
    for name, predicate in COMPILE_CURSOR_CHECKS.items():
        op.create_check_constraint(name, "compile_cursors", predicate)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    for name in COMPILE_CURSOR_CHECKS:
        op.drop_constraint(name, "compile_cursors", type_="check")
    # Rows without a committed position only carry a pending intent; the 0004 schema
    # cannot represent them, and an absent cursor row is the equivalent state there.
    op.execute("DELETE FROM compile_cursors WHERE last_wal_id IS NULL")
    op.drop_column("compile_cursors", "pending_plan")
    op.drop_column("compile_cursors", "pending_plan_hash")
    op.drop_column("compile_cursors", "pending_wal_last_id")
    op.alter_column("compile_cursors", "last_compile_hash", existing_type=sa.String(64), nullable=False)
    op.alter_column("compile_cursors", "last_wal_id", existing_type=sa.String(26), nullable=False)
    op.drop_constraint("compile_cursors_workspace_uuid_fkey", "compile_cursors", type_="foreignkey")

    for name in COMPILE_RUN_CHECKS:
        op.drop_constraint(name, "compile_runs", type_="check")
    for name in WORKSPACE_CHECKS:
        op.drop_constraint(name, "workspaces", type_="check")
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"])
