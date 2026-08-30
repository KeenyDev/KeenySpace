"""E2E backup -> wipe -> restore roundtrip preserves DB + FS state.

Asserts that workspaces row count + FS file hashes survive a full
backup/restore cycle (modulo created_at timestamps which pg_dump preserves
verbatim, so they're stable here).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

PG_URL = os.environ.get("KEENYSPACE_DB__URL")
HAS_PG_DUMP = shutil.which("pg_dump") is not None and shutil.which("psql") is not None

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"
    ),
    pytest.mark.skipif(not HAS_PG_DUMP, reason="pg_dump/psql binary unavailable"),
]


async def _reset_schema(pg_url: str) -> None:
    import sqlalchemy as sa

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


async def _seed_api_key_post_lifespan() -> tuple[str, str]:
    import base64
    import hashlib as _h
    import secrets

    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session

    pepper = get_settings().auth.api_key_pepper
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = _h.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"rt-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "rt", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'rt', "
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


def _hash_fs_root_workspaces(fs_root: Path) -> dict[str, str]:
    """Return path -> sha256 for every regular file under workspaces/."""
    out: dict[str, str] = {}
    ws_dir = fs_root / "workspaces"
    if not ws_dir.exists():
        return out
    for p in sorted(ws_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(fs_root)
            out[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


async def _workspace_count(pg_url: str) -> int:
    import sqlalchemy as sa

    eng = create_async_engine(pg_url)
    async with eng.connect() as conn:
        row = await conn.execute(sa.text("SELECT count(*) FROM workspaces"))
        n = row.scalar_one()
    await eng.dispose()
    return int(n)


async def test_backup_wipe_restore_preserves_workspaces_and_pg_state(
    app: Any, pg_url: str, fs_root: Any
) -> None:
    """Seed two workspaces, backup, force-restore, assert state equivalence."""

    await _reset_schema(pg_url)
    start = time.perf_counter()
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            # Seed two workspaces
            slugs: list[str] = []
            for _ in range(2):
                slug = f"rt-{uuid4().hex[:8]}"
                resp = await client.post(
                    "/v1/api/workspaces/",
                    json={"slug": slug, "blueprint": "default"},
                )
                assert resp.status_code == 201, resp.text
                slugs.append(slug)

            before_fs_hashes = _hash_fs_root_workspaces(Path(fs_root))
            before_ws_count = await _workspace_count(pg_url)
            assert before_ws_count == 2

            # Backup
            backup_resp = await client.post("/v1/admin/backup")
            assert backup_resp.status_code == 200
            backup_bytes = backup_resp.content
            assert len(backup_bytes) > 0

            # Force restore — wipes existing then re-applies
            restore_resp = await client.post(
                "/v1/admin/restore",
                params={"force": "true"},
                files={"file": (
                    "backup.tar.gz", backup_bytes, "application/gzip"
                )},
            )
            assert restore_resp.status_code == 200, restore_resp.text
            assert restore_resp.json()["wiped"] is True

            after_fs_hashes = _hash_fs_root_workspaces(Path(fs_root))
            after_ws_count = await _workspace_count(pg_url)

    elapsed = time.perf_counter() - start
    assert after_ws_count == before_ws_count, (
        f"workspace count drift: {before_ws_count} -> {after_ws_count}"
    )
    assert after_fs_hashes == before_fs_hashes, (
        f"FS content drift after roundtrip; "
        f"added: {set(after_fs_hashes) - set(before_fs_hashes)}, "
        f"removed: {set(before_fs_hashes) - set(after_fs_hashes)}"
    )
    # Record wall-clock for SUMMARY (smallest test workspace shape).
    print(f"ROUNDTRIP_WALLCLOCK_SEC={elapsed:.3f}")


async def _wipe_target_state(pg_url: str, fs_root: Path) -> None:
    """Emulate `docker compose down -v` + a fresh boot: empty rows, empty FS.

    The schema stays (Alembic runs on boot); only the data is gone.
    """
    import sqlalchemy as sa
    from keenyspace_server.api.admin import PG_TABLES_FK_ORDER

    eng = create_async_engine(pg_url)
    async with eng.begin() as conn:
        for table in PG_TABLES_FK_ORDER:
            await conn.execute(sa.text(f"DELETE FROM {table}"))
    await eng.dispose()
    shutil.rmtree(fs_root / "workspaces", ignore_errors=True)
    shutil.rmtree(fs_root / "blueprints", ignore_errors=True)


async def test_restore_without_force_into_empty_target(
    app: Any, pg_url: str, fs_root: Any
) -> None:
    """An unforced restore into an empty target must complete, not block.

    Regression for the drill hang: the endpoint's own session held ACCESS SHARE
    locks from its pre-flight reads while psql replayed a `--clean` dump, whose
    DROP TABLE waits for ACCESS EXCLUSIVE. The force branch committed before
    psql and survived; this path did not and hung until the client timed out.
    """

    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            slug = f"rt-{uuid4().hex[:8]}"
            resp = await client.post(
                "/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"}
            )
            assert resp.status_code == 201, resp.text

            backup_resp = await client.post("/v1/admin/backup")
            assert backup_resp.status_code == 200
            backup_bytes = backup_resp.content

        await _wipe_target_state(pg_url, Path(fs_root))
        # api_keys went with the wipe, exactly as they do after `down -v`.
        _, replaint = await _seed_api_key_post_lifespan()
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"Authorization": f"Bearer {replaint}"},
        ) as client:
            restore_resp = await asyncio.wait_for(
                client.post(
                    "/v1/admin/restore",
                    files={
                        "file": ("backup.tar.gz", backup_bytes, "application/gzip")
                    },
                ),
                timeout=120,
            )
            assert restore_resp.status_code == 200, restore_resp.text
            assert restore_resp.json()["wiped"] is False

        assert await _workspace_count(pg_url) == 1


# Reference unused imports so ruff stays happy when pg unavailable
_ = io
_ = tarfile
