"""auth: users group snapshot for API-key authorization, api_keys expiry

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("groups", postgresql.JSONB(), nullable=True))
    op.add_column(
        "users", sa.Column("groups_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "api_keys", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("api_keys", "expires_at")
    op.drop_column("users", "groups_seen_at")
    op.drop_column("users", "groups")
