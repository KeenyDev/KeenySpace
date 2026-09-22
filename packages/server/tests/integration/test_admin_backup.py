"""POST /v1/admin/backup streaming tarball integration tests (Phase 5 ADMIN-01).

Real Postgres; pg_dump invoked via subprocess. If pg_dump is unavailable in the
test environment the tests skip cleanly.
"""

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
HAS_PG_DUMP = shutil.which("pg_dump") is not None

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"
    ),
    pytest.mark.skipif(not HAS_PG_DUMP, reason="pg_dump binary unavailable"),
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
    user_sub = f"admin-backup-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "admin", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'admin-backup', "
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


async def _seed_workspace(client: AsyncClient) -> str:
    slug = f"backup-{uuid4().hex[:8]}"
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"}
    )
    assert resp.status_code == 201, resp.text
    return slug


async def _collect_tarball(resp_content: bytes) -> tuple[list[str], dict[str, bytes]]:
    names: list[str] = []
    contents: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(resp_content), mode="r:gz") as tar:
        for info in tar:
            names.append(info.name)
            if info.isfile():
                fp = tar.extractfile(info)
                if fp is not None:
                    contents[info.name] = fp.read()
    return names, contents


async def test_admin_backup_streams_tarball(app: Any, pg_url: str) -> None:
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
            resp = await client.post("/v1/admin/backup")
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"] == "application/gzip"
            assert "attachment" in resp.headers["content-disposition"]
            names, _ = await _collect_tarball(resp.content)
            assert names[0] == "manifest.json", names
            assert "pg_dump.sql" in names, names
            assert any(n.startswith("fs_root/workspaces/") for n in names), names


async def test_admin_backup_manifest_shape(app: Any, pg_url: str) -> None:
    from keenyspace_shared.mcp_contracts import BackupManifest

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
            resp = await client.post("/v1/admin/backup")
            assert resp.status_code == 200
            _, contents = await _collect_tarball(resp.content)
            manifest = BackupManifest.model_validate_json(contents["manifest.json"])
            assert manifest.keenyspace_version
            assert manifest.alembic_head and manifest.alembic_head != "unknown"
            assert manifest.workspaces["count"] >= 1
            assert "workspaces" in manifest.pg_tables_dumped


async def test_admin_backup_excludes_obsidian(
    app: Any, pg_url: str, fs_root: Any
) -> None:
    from pathlib import Path

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
            slug = await _seed_workspace(client)
            # locate ws_dir and drop a .obsidian/ file
            from keenyspace_server.db.models import Workspace
            from keenyspace_server.db.session import get_db_session

            async with get_db_session() as session:
                ws = (
                    await session.execute(
                        select(Workspace).where(Workspace.slug == slug)
                    )
                ).scalar_one()
                ws_uuid = str(ws.uuid)
            ws_dir = Path(fs_root) / "workspaces" / ws_uuid
            obsidian = ws_dir / ".obsidian"
            obsidian.mkdir(parents=True, exist_ok=True)
            (obsidian / "workspace.json").write_text("{}")

            resp = await client.post("/v1/admin/backup")
            assert resp.status_code == 200
            names, _ = await _collect_tarball(resp.content)
            assert all(".obsidian" not in n.split("/") for n in names), names


async def test_admin_backup_increments_counters(
    app: Any, pg_url: str
) -> None:
    from keenyspace_server.observability.metrics import (
        ADMIN_BACKUP_BYTES,
        ADMIN_BACKUP_TOTAL,
    )

    await _reset_schema(pg_url)
    before_total = ADMIN_BACKUP_TOTAL._value.get()
    before_bytes = ADMIN_BACKUP_BYTES._value.get()
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
            resp = await client.post("/v1/admin/backup")
            assert resp.status_code == 200
            # drain content to trigger streaming generator
            assert len(resp.content) > 0
    after_total = ADMIN_BACKUP_TOTAL._value.get()
    after_bytes = ADMIN_BACKUP_BYTES._value.get()
    assert after_total > before_total
    assert after_bytes > before_bytes


async def test_admin_backup_audit_log_row(app: Any, pg_url: str) -> None:
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
            resp = await client.post("/v1/admin/backup")
            assert resp.status_code == 200

            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.action == "admin.backup.requested"
                        )
                    )
                ).scalars().all()
            assert rows
            row = rows[0]
            assert row.actor_sub == user_sub
            payload = row.payload
            assert isinstance(payload, dict)
            assert payload.get("workspace_count") == 1


async def test_admin_backup_anonymous_401(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            resp = await client.post("/v1/admin/backup")
            assert resp.status_code == 401


async def test_admin_backup_pg_dump_failure_returns_500(
    app: Any, pg_url: str, monkeypatch: Any
) -> None:
    """A pg_dump non-zero exit must surface as 500, not a silent empty 200.

    Regression for the CI version-mismatch failure (server 17.2 vs pg_dump
    16.14): pg_dump ran inside the StreamingResponse generator, so the 200 +
    headers were already on the wire and the failure truncated the body to an
    empty tarball (tarfile.ReadError) instead of returning an error.
    """
    import keenyspace_server.api.admin as admin_mod

    monkeypatch.setattr(
        admin_mod,
        "_pg_dump_argv",
        lambda _db_url: ["sh", "-c", "echo 'server version mismatch' >&2; exit 1"],
    )
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
            resp = await client.post("/v1/admin/backup")
            assert resp.status_code == 500, resp.text
            assert resp.json()["detail"]["error"] == "pg_dump_failed"


# Silence unused-import warning in environments without pg_dump
_ = json
