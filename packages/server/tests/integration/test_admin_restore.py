"""POST /v1/admin/restore happy-path + version/schema/target checks (Phase 5 ADMIN-02)."""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

PG_URL = os.environ.get("KEENYSPACE_DB__URL")
HAS_PG_DUMP = shutil.which("pg_dump") is not None and shutil.which("psql") is not None

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
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
    import hashlib
    import secrets

    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session

    pepper = get_settings().auth.api_key_pepper.get_secret_value()
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"admin-restore-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at, "
                "groups, groups_seen_at) VALUES (:sub, :dn, NULL, 'api_key', :now, "
                "CAST(:groups AS jsonb), :now)"
            ),
            {"groups": '["keenyspace-admins"]', "sub": user_sub, "dn": "admin-restore", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'admin-restore', "
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


async def _current_alembic_head() -> str:
    from keenyspace_server.db.session import get_db_session

    async with get_db_session() as session:
        row = await session.execute(text("SELECT version_num FROM alembic_version"))
        return row.scalar_one()


async def _capture_backup(
    client: AsyncClient,
) -> bytes:
    resp = await client.post("/v1/admin/backup")
    assert resp.status_code == 200, resp.text
    return resp.content


async def _seed_workspace(client: AsyncClient) -> str:
    slug = f"restore-{uuid4().hex[:8]}"
    resp = await client.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
    assert resp.status_code == 201, resp.text
    return slug


def _make_tarball(manifest_dict: dict[str, Any], pg_dump_bytes: bytes) -> bytes:
    """Build a minimal in-memory tarball with manifest + pg_dump + an empty
    fs_root/workspaces/<uuid>/ subtree."""
    buf = io.BytesIO()
    manifest_bytes = json.dumps(manifest_dict, default=str).encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        mi = tarfile.TarInfo(name="manifest.json")
        mi.size = len(manifest_bytes)
        mi.mtime = int(datetime.now(UTC).timestamp())
        tar.addfile(mi, io.BytesIO(manifest_bytes))

        pi = tarfile.TarInfo(name="pg_dump.sql")
        pi.size = len(pg_dump_bytes)
        pi.mtime = int(datetime.now(UTC).timestamp())
        tar.addfile(pi, io.BytesIO(pg_dump_bytes))

        # fs_root/workspaces directory entry
        di = tarfile.TarInfo(name="fs_root/workspaces")
        di.type = tarfile.DIRTYPE
        di.mode = 0o755
        di.mtime = int(datetime.now(UTC).timestamp())
        tar.addfile(di)
    return buf.getvalue()


def _default_manifest(keenyspace_version: str, alembic_head: str) -> dict[str, Any]:
    return {
        "version": 1,
        "keenyspace_version": keenyspace_version,
        "schema_version": 1,
        "alembic_head": alembic_head,
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": "test",
        "fs_root_size_bytes": 0,
        "workspaces": {"count": 0, "uuids": []},
        "blueprints": {"count": 0, "names": []},
        "pg_tables_dumped": ["alembic_version"],
    }


_EMPTY_PG_DUMP = b"-- no-op\nSELECT 1;\n"


async def test_restore_version_mismatch_returns_422(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        head = await _current_alembic_head()
        manifest = _default_manifest(keenyspace_version="0.2.0", alembic_head=head)
        tarball = _make_tarball(manifest, _EMPTY_PG_DUMP)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            resp = await client.post(
                "/v1/admin/restore",
                files={"file": ("backup.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            assert detail["error"] == "version_mismatch"


async def test_restore_schema_mismatch_returns_422(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        manifest = _default_manifest(keenyspace_version="0.1.0", alembic_head="0001_bogus")
        tarball = _make_tarball(manifest, _EMPTY_PG_DUMP)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            resp = await client.post(
                "/v1/admin/restore",
                files={"file": ("backup.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            assert detail["error"] == "schema_mismatch"


async def test_restore_target_not_empty_returns_409(app: Any, pg_url: str) -> None:
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
            await _seed_workspace(client)
            head = await _current_alembic_head()
            manifest = _default_manifest(keenyspace_version="0.1.0", alembic_head=head)
            tarball = _make_tarball(manifest, _EMPTY_PG_DUMP)
            resp = await client.post(
                "/v1/admin/restore",
                files={"file": ("backup.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert detail["error"] == "target_not_empty"
            assert detail["existing_workspaces"] >= 1


async def test_restore_force_wipes_existing(app: Any, pg_url: str) -> None:
    from keenyspace_server.observability.metrics import ADMIN_RESTORE_WIPED_TOTAL

    await _reset_schema(pg_url)
    before_wipe = ADMIN_RESTORE_WIPED_TOTAL._value.get()
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
            await _seed_workspace(client)
            head = await _current_alembic_head()
            manifest = _default_manifest(keenyspace_version="0.1.0", alembic_head=head)
            tarball = _make_tarball(manifest, _EMPTY_PG_DUMP)
            resp = await client.post(
                "/v1/admin/restore",
                params={"force": "true"},
                files={"file": ("backup.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["ok"] is True
            assert payload["wiped"] is True
    after_wipe = ADMIN_RESTORE_WIPED_TOTAL._value.get()
    assert after_wipe == before_wipe + 1


async def test_restore_force_wipe_audit_log_row(app: Any, pg_url: str) -> None:
    from keenyspace_server.db.models import AuditLog
    from keenyspace_server.db.session import get_db_session

    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        user_sub, plaintext = await _seed_api_key_post_lifespan()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            await _seed_workspace(client)
            head = await _current_alembic_head()
            manifest = _default_manifest(keenyspace_version="0.1.0", alembic_head=head)
            tarball = _make_tarball(manifest, _EMPTY_PG_DUMP)
            resp = await client.post(
                "/v1/admin/restore",
                params={"force": "true"},
                files={"file": ("backup.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 200

            async with get_db_session() as session:
                wipe_rows = (
                    (
                        await session.execute(
                            select(AuditLog).where(AuditLog.action == "admin.restore.wipe")
                        )
                    )
                    .scalars()
                    .all()
                )
                applied_rows = (
                    (
                        await session.execute(
                            select(AuditLog).where(AuditLog.action == "admin.restore.applied")
                        )
                    )
                    .scalars()
                    .all()
                )
            assert wipe_rows, "admin.restore.wipe row missing"
            assert applied_rows, "admin.restore.applied row missing"
            assert wipe_rows[0].actor_sub == user_sub
            assert applied_rows[0].payload.get("wiped") is True


async def test_restore_happy_path_empty_target(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        head = await _current_alembic_head()
        manifest = _default_manifest(keenyspace_version="0.1.0", alembic_head=head)
        manifest["workspaces"] = {"count": 0, "uuids": []}
        tarball = _make_tarball(manifest, _EMPTY_PG_DUMP)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            resp = await client.post(
                "/v1/admin/restore",
                files={"file": ("backup.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["ok"] is True
            assert payload["workspaces_restored"] == 0
            assert payload["wiped"] is False
