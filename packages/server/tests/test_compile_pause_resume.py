from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
]


async def _seed_paused_workspace(slug: str) -> None:
    from keenyspace_server.db.models import Workspace
    from keenyspace_server.db.session import get_db_session

    async with get_db_session() as session:
        ws = (await session.execute(select(Workspace).where(Workspace.slug == slug))).scalar_one()
        await session.execute(
            update(Workspace)
            .where(Workspace.uuid == ws.uuid)
            .values(
                compile_state="paused",
                compile_paused_reason="loop_abort",
                compile_paused_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _seed(client: AsyncClient) -> str:
    slug = f"pause-resume-{uuid4().hex[:8]}"
    resp = await client.post(
        "/v1/api/workspaces/",
        json={"slug": slug, "blueprint": "default"},
    )
    assert resp.status_code == 201, resp.text
    return slug


async def _reset_schema(pg_url: str) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


async def _seed_api_key_post_lifespan() -> tuple[str, str]:
    import base64
    import hashlib
    import secrets
    from uuid import uuid4 as _uuid4

    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session
    from sqlalchemy import text

    pepper = get_settings().auth.api_key_pepper
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"pause-{_uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "pause", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'pause', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": _uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
        await session.commit()

    return user_sub, f"ks_live_{body}"


async def test_post_compile_returns_409_when_paused(app, pg_url) -> None:
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
            slug = await _seed(client)
            await _seed_paused_workspace(slug)

            resp = await client.post(f"/v1/api/workspaces/{slug}/compile")
            assert resp.status_code == 409, resp.text
            body = resp.json()
            assert body["detail"]["paused_reason"] == "loop_abort"
            assert body["detail"]["paused_at"] is not None


async def test_post_compile_resume_resets_state(app, pg_url) -> None:
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
            slug = await _seed(client)
            await _seed_paused_workspace(slug)

            r = await client.post(f"/v1/api/workspaces/{slug}/compile/resume")
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "idle"
            assert r.json()["paused_reason"] is None

            r2 = await client.post(f"/v1/api/workspaces/{slug}/compile/resume")
            assert r2.status_code == 200
            assert r2.json()["state"] == "idle"


async def test_get_compile_status_returns_state(app, pg_url) -> None:
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
            slug = await _seed(client)

            r = await client.get(f"/v1/api/workspaces/{slug}/compile/status")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["state"] == "idle"
            assert body["last_wal_id"] is None
            assert body["paused_reason"] is None
