"""Phase 4 archive lifecycle integration tests (WS-05 / D-01..D-03).

Full lifespan + real Postgres; uses ASGITransport with API-key Bearer auth.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
]


async def _reset_schema(pg_url: str) -> None:
    import sqlalchemy as sa

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


async def _seed_api_key_post_lifespan() -> tuple[str, str]:
    """Verbatim from test_compile_pause_resume.py:58-99 — seeds user + api key."""
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
    user_sub = f"archive-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "archive", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'archive', 'ks_live_', :h, :lh, :now)"
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
    slug = f"archive-{uuid4().hex[:8]}"
    resp = await client.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
    assert resp.status_code == 201, resp.text
    return slug


async def _ws_row(slug: str) -> Any:
    from keenyspace_server.db.models import Workspace
    from keenyspace_server.db.session import get_db_session

    async with get_db_session() as session:
        return (
            await session.execute(select(Workspace).where(Workspace.slug == slug))
        ).scalar_one()


async def test_archive_flips_db_and_config(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    import yaml

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
            resp = await client.post(f"/v1/api/workspaces/{slug}/archive")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "archived"
            assert body["archived_at"] is not None

            ws = await _ws_row(slug)
            assert ws.status == "archived"
            assert ws.archived_at is not None
            assert ws.compile_paused_reason == "archived"
            assert ws.compile_state == "paused"

            from keenyspace_server.config import get_settings

            ws_dir = get_settings().fs.root / "workspaces" / str(ws.uuid)
            config_data = yaml.safe_load((ws_dir / ".keenyspace" / "config.yaml").read_text())
            assert "archived_at" in config_data

            from keenyspace_server.db.models import AuditLog
            from keenyspace_server.db.session import get_db_session

            async with get_db_session() as session:
                audit = (
                    await session.execute(
                        select(AuditLog).where(AuditLog.action == "workspace.archived")
                    )
                ).scalars().all()
            assert any(row.workspace_uuid == ws.uuid for row in audit)


async def test_double_archive_409(app, pg_url) -> None:  # type: ignore[no-untyped-def]
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
            first = await client.post(f"/v1/api/workspaces/{slug}/archive")
            assert first.status_code == 200
            second = await client.post(f"/v1/api/workspaces/{slug}/archive")
            assert second.status_code == 409
            assert second.json()["detail"]["code"] == "workspace_already_archived"


async def test_unarchive_resumes_compile(app, pg_url) -> None:  # type: ignore[no-untyped-def]
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
            await client.post(f"/v1/api/workspaces/{slug}/archive")
            resp = await client.post(f"/v1/api/workspaces/{slug}/unarchive")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "active"
            assert resp.json()["archived_at"] is None

            ws = await _ws_row(slug)
            assert ws.status == "active"
            assert ws.archived_at is None
            assert ws.compile_paused_reason is None
            assert ws.compile_state == "idle"


async def test_unarchive_preserves_non_archived_pause(app, pg_url) -> None:  # type: ignore[no-untyped-def]
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
            ws = await _ws_row(slug)

            from keenyspace_server.db.models import Workspace as _Workspace
            from keenyspace_server.db.session import get_db_session

            now = datetime.now(UTC)
            async with get_db_session() as session:
                await session.execute(
                    update(_Workspace)
                    .where(_Workspace.uuid == ws.uuid)
                    .values(
                        status="archived",
                        archived_at=now,
                        compile_state="paused",
                        compile_paused_reason="daily_ceiling",
                        compile_paused_at=now,
                    )
                )
                await session.commit()

            resp = await client.post(f"/v1/api/workspaces/{slug}/unarchive")
            assert resp.status_code == 200, resp.text

            ws_after = await _ws_row(slug)
            assert ws_after.status == "active"
            assert ws_after.compile_paused_reason == "daily_ceiling"


async def test_wal_rejects_archived(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    from keenyspace_server.config import get_settings
    from keenyspace_server.wal import writer as wal_writer
    from keenyspace_server.wal.locks import WorkspaceLockRegistry

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
            await client.post(f"/v1/api/workspaces/{slug}/archive")

            ws = await _ws_row(slug)
            settings = get_settings()
            ws_root = settings.fs.root / "workspaces" / str(ws.uuid)
            locks = WorkspaceLockRegistry()
            with pytest.raises(wal_writer.WorkspaceArchivedError):
                await wal_writer.append_log(
                    ws_uuid=ws.uuid,
                    ws_root=ws_root,
                    content="should be rejected",
                    actor="test",
                    source="test",
                    client_version=None,
                    settings=settings,
                    locks=locks,
                )


async def test_archive_mid_compile_race(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    from keenyspace_server.db.models import Workspace as _Workspace
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
            ws = await _ws_row(slug)

            async with get_db_session() as session:
                await session.execute(
                    update(_Workspace)
                    .where(_Workspace.uuid == ws.uuid)
                    .values(compile_state="running")
                )
                await session.commit()

            resp = await client.post(f"/v1/api/workspaces/{slug}/archive")
            assert resp.status_code == 200, resp.text

            ws_after = await _ws_row(slug)
            # Archive UPDATE wins; the (synthetic) "running" state is overwritten
            # to paused/archived. A real running task self-terminates on next
            # DB read per RESEARCH §Pattern 2.
            assert ws_after.compile_state == "paused"
            assert ws_after.compile_paused_reason == "archived"


async def test_archive_unauthenticated_401(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            resp = await client.post("/v1/api/workspaces/some-slug/archive")
            assert resp.status_code == 401
