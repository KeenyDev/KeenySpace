"""auth: add lookup_hash to api_keys for O(1) API-key lookup

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(sa.text("SELECT COUNT(*) FROM api_keys")).scalar_one()
    if row != 0:
        raise RuntimeError(
            f"api_keys has {row} row(s); 0003 expects empty table — manual cleanup required"
        )
    op.add_column(
        "api_keys",
        sa.Column("lookup_hash", sa.String(64), nullable=False, unique=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "lookup_hash")
