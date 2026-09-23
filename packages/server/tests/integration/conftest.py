"""Helpers shared by more than one integration module.

The alembic-driving trio (test_compile_alembic, test_migration_0005,
test_models_migrations_parity) shells out to ``uv run alembic`` and needs the server
package root as cwd plus an empty ``public`` schema to start from. The seeding helpers
below are shared by the manifest and pages endpoint tests, which both need an API key
minted after the app lifespan has opened the DB pool.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import text

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

SERVER_DIR = Path(__file__).resolve().parents[2]
"""packages/server — the cwd `alembic.ini` is resolved against."""


def _reset_schema() -> None:
    """Start from an empty public schema.

    These tests drive alembic against the shared CI database. Other tests seed
    rows (e.g. api_keys, which migration 0003 guards against being non-empty)
    and leave partial schema state, so a bare upgrade/downgrade cycle is not
    reproducible without first wiping the schema.
    """
    import asyncio

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _drop() -> None:
        eng = create_async_engine(PG_URL or "", isolation_level="AUTOCOMMIT")
        async with eng.connect() as conn:
            await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            await conn.execute(sa.text("CREATE SCHEMA public"))
        await eng.dispose()

    asyncio.run(_drop())


async def _seed_api_key_post_lifespan() -> tuple[str, str]:
    import base64
    import hashlib as _h
    import secrets

    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session

    pepper = get_settings().auth.api_key_pepper.get_secret_value()
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = _h.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"manifest-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "manifest", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'manifest', "
                "'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
        await session.commit()

    return user_sub, f"ks_live_{body}"


async def _seed_workspace(client: AsyncClient, slug: str | None = None) -> str:
    slug = slug or f"mf-{uuid4().hex[:8]}"
    resp = await client.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
    assert resp.status_code == 201, resp.text
    return slug


def _workspace_dir(app, slug: str) -> Path:
    from keenyspace_server.db.models import Workspace as _Workspace  # noqa: F401

    # Walk fs_root/workspaces, picking the dir whose .keenyspace/config.yaml mentions slug.
    fs_root = Path(app.state.settings.fs.root) / "workspaces"
    for entry in fs_root.iterdir():
        cfg = entry / ".keenyspace" / "config.yaml"
        if cfg.is_file() and f"slug: {slug}" in cfg.read_text():
            return entry
    raise AssertionError(f"workspace dir for {slug!r} not found under {fs_root}")
