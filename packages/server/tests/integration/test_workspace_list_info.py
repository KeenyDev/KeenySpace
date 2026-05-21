from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
]


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
    user_sub = f"list-{_uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "list", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'list', 'ks_live_', :h, :lh, :now)"
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


async def _archive_workspace_direct(slug: str) -> None:
    from keenyspace_server.db.models import Workspace
    from keenyspace_server.db.session import get_db_session
    from sqlalchemy import select, update

    async with get_db_session() as session:
        ws = (await session.execute(select(Workspace).where(Workspace.slug == slug))).scalar_one()
        await session.execute(
            update(Workspace)
            .where(Workspace.uuid == ws.uuid)
            .values(
                status="archived",
                archived_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def test_list_workspaces_default_active_only(app, pg_url) -> None:
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

            for i in range(2):
                resp = await client.post(
                    "/v1/api/workspaces/",
                    json={"slug": f"active-{i}-{uuid4().hex[:6]}", "blueprint": "default"},
                )
                assert resp.status_code == 201, resp.text

            arc_slug = f"arc-{uuid4().hex[:6]}"
            resp = await client.post(
                "/v1/api/workspaces/",
                json={"slug": arc_slug, "blueprint": "default"},
            )
            assert resp.status_code == 201, resp.text
            await _archive_workspace_direct(arc_slug)

            r = await client.get("/v1/api/workspaces/")
            assert r.status_code == 200, r.text
            data = r.json()
            assert len(data["workspaces"]) == 2
            assert all(w["status"] == "active" for w in data["workspaces"])


async def test_list_workspaces_status_archived(app, pg_url) -> None:
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

            active_slug = f"active-{uuid4().hex[:6]}"
            await client.post(
                "/v1/api/workspaces/",
                json={"slug": active_slug, "blueprint": "default"},
            )

            arc_slug = f"arc-{uuid4().hex[:6]}"
            resp = await client.post(
                "/v1/api/workspaces/",
                json={"slug": arc_slug, "blueprint": "default"},
            )
            assert resp.status_code == 201, resp.text
            await _archive_workspace_direct(arc_slug)

            r = await client.get("/v1/api/workspaces/?status=archived")
            assert r.status_code == 200, r.text
            data = r.json()
            assert len(data["workspaces"]) == 1
            assert data["workspaces"][0]["status"] == "archived"
            assert data["workspaces"][0]["slug"] == arc_slug


async def test_list_workspaces_status_all(app, pg_url) -> None:
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

            for i in range(2):
                await client.post(
                    "/v1/api/workspaces/",
                    json={"slug": f"all-active-{i}-{uuid4().hex[:6]}", "blueprint": "default"},
                )

            arc_slug = f"arc-all-{uuid4().hex[:6]}"
            resp = await client.post(
                "/v1/api/workspaces/",
                json={"slug": arc_slug, "blueprint": "default"},
            )
            assert resp.status_code == 201, resp.text
            await _archive_workspace_direct(arc_slug)

            r = await client.get("/v1/api/workspaces/?status=all")
            assert r.status_code == 200, r.text
            data = r.json()
            assert len(data["workspaces"]) == 3


async def test_list_workspaces_status_invalid_422(app, pg_url) -> None:
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

            r = await client.get("/v1/api/workspaces/?status=foo")
            assert r.status_code == 422, r.text


async def test_list_workspaces_cursor_pagination(app, pg_url) -> None:
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

            slugs = [f"page-ws-{i:02d}-{uuid4().hex[:4]}" for i in range(5)]
            for slug in slugs:
                resp = await client.post(
                    "/v1/api/workspaces/",
                    json={"slug": slug, "blueprint": "default"},
                )
                assert resp.status_code == 201, resp.text

            all_collected: list[str] = []
            cursor: str | None = None
            pages = 0
            while True:
                url = "/v1/api/workspaces/?status=all&limit=2"
                if cursor:
                    url += f"&cursor={cursor}"
                r = await client.get(url)
                assert r.status_code == 200, r.text
                data = r.json()
                batch = data["workspaces"]
                all_collected.extend(w["slug"] for w in batch)
                cursor = data.get("next_cursor")
                pages += 1
                if cursor is None:
                    break
                assert len(batch) > 0

            assert len(all_collected) == 5
            assert len(set(all_collected)) == 5, "no duplicates"
            assert pages >= 3


async def test_get_workspace_info_returns_metadata(app, pg_url) -> None:
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

            slug = f"info-ws-{uuid4().hex[:6]}"
            resp = await client.post(
                "/v1/api/workspaces/",
                json={"slug": slug, "blueprint": "default"},
            )
            assert resp.status_code == 201, resp.text

            r = await client.get(f"/v1/api/workspaces/{slug}")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["slug"] == slug
            assert data["status"] == "active"
            assert "uuid" in data
            assert "blueprint_pin" in data
            assert "compile_state" in data
            assert isinstance(data["page_count"], int)
            assert data["page_count"] >= 0
            assert data["archived_at"] is None


async def test_get_workspace_info_404(app, pg_url) -> None:
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

            r = await client.get("/v1/api/workspaces/no-such-workspace-xyz")
            assert r.status_code == 404, r.text


async def test_list_workspaces_unauthenticated_401(app, pg_url) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")

            r = await client.get("/v1/api/workspaces/")
            assert r.status_code == 401, r.text
