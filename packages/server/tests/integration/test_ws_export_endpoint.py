"""Phase 4 workspace export endpoint integration tests (WS-04 / WS-06 / D-06).

Full lifespan + real Postgres; uses ASGITransport with API-key Bearer auth.
"""
from __future__ import annotations

import io
import os
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"
    ),
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

    pepper = get_settings().auth.api_key_pepper
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"export-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "export", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'export', "
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
    slug = f"export-{uuid4().hex[:8]}"
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"}
    )
    assert resp.status_code == 201, resp.text
    return slug


async def test_export_returns_application_zip_with_attachment_disposition(
    app, pg_url
) -> None:
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
            resp = await client.get(f"/v1/api/workspaces/{slug}/export")
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"] == "application/zip"
            assert (
                resp.headers["content-disposition"]
                == f'attachment; filename="{slug}.zip"'
            )
            body = resp.content
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = set(zf.namelist())
                assert "index.md" in names, names


async def test_export_unknown_slug_returns_404(app, pg_url) -> None:
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
            resp = await client.get("/v1/api/workspaces/does-not-exist/export")
            assert resp.status_code == 404


async def test_export_unauthenticated_401(app, pg_url) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            resp = await client.get("/v1/api/workspaces/some-slug/export")
            assert resp.status_code == 401


async def test_export_archived_workspace_succeeds_and_audit_records_flag(
    app, pg_url
) -> None:
    from keenyspace_server.db.models import AuditLog
    from keenyspace_server.db.session import get_db_session

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
            arch = await client.post(f"/v1/api/workspaces/{slug}/archive")
            assert arch.status_code == 200, arch.text

            resp = await client.get(f"/v1/api/workspaces/{slug}/export")
            assert resp.status_code == 200, resp.text

            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.action == "workspace.exported"
                        )
                    )
                ).scalars().all()
            assert rows, "expected workspace.exported audit row"
            payloads = [r.payload for r in rows]
            assert any(
                isinstance(p, dict) and p.get("archived") is True
                for p in payloads
            ), payloads


async def test_export_audit_log_emitted_for_active_workspace(app, pg_url) -> None:
    from keenyspace_server.db.models import AuditLog
    from keenyspace_server.db.session import get_db_session

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
            resp = await client.get(f"/v1/api/workspaces/{slug}/export")
            assert resp.status_code == 200

            async with get_db_session() as session:
                rows = (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.action == "workspace.exported"
                        )
                    )
                ).scalars().all()
            assert rows
            payloads = [r.payload for r in rows]
            assert all(
                isinstance(p, dict) and p.get("archived") is False
                for p in payloads
            ), payloads
