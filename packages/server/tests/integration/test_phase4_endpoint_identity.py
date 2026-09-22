"""Phase 4 UAT G-2 regression: identity resolution under real AuthMiddleware.

Drives each of the four endpoints added in Plans 04-02 (archive/unarchive),
04-07 (export), 04-08 (import) end-to-end through Starlette
AuthenticationMiddleware + CompositeAuthBackend + Bearer ks_live_* token, and
asserts that none of them return HTTP 500 due to mis-read identity.

NOTE: this test deliberately does NOT skip on health 500/503. That escape hatch
in pre-UAT integration tests is what hid the original bug. If health is not
green we want a hard failure surfacing the response body.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
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
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session

    pepper = get_settings().auth.api_key_pepper.get_secret_value()
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"g2-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "g2", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'g2', "
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
    slug = f"g2-{uuid4().hex[:8]}"
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"}
    )
    assert resp.status_code == 201, resp.text
    return slug


def _make_min_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.md", b"# imported\n")
    return buf.getvalue()


async def _assert_audit_row(action: str, expected_sub: str) -> None:
    from keenyspace_server.db.models import AuditLog
    from keenyspace_server.db.session import get_db_session

    async with get_db_session() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == action)
            )
        ).scalars().all()
    assert rows, f"expected at least one {action!r} audit row"
    actor_subs = {r.actor_sub for r in rows}
    assert expected_sub in actor_subs, (
        f"{action} audit row actor_sub mismatch: "
        f"expected {expected_sub!r}, got {actor_subs!r}"
    )


def _make_client(app: object, plaintext: str) -> AsyncClient:
    transport = ASGITransport(app=app, raise_app_exceptions=False)  # type: ignore[arg-type]
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plaintext}"},
    )


async def _require_healthy(client: AsyncClient) -> None:
    health = await client.get("/healthz")
    if health.status_code != 200:
        pytest.fail(
            f"/healthz not green: status={health.status_code} body={health.text}"
        )


async def test_archive_endpoint_resolves_identity_under_real_authmiddleware(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        user_sub, plaintext = await _seed_api_key_post_lifespan()
        async with _make_client(app, plaintext) as client:
            await _require_healthy(client)
            slug = await _seed_workspace(client)
            resp = await client.post(f"/v1/api/workspaces/{slug}/archive")
            assert resp.status_code == 200, (
                f"archive returned {resp.status_code} body={resp.text}"
            )
            await _assert_audit_row("workspace.archived", user_sub)


async def test_unarchive_endpoint_resolves_identity_under_real_authmiddleware(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        user_sub, plaintext = await _seed_api_key_post_lifespan()
        async with _make_client(app, plaintext) as client:
            await _require_healthy(client)
            slug = await _seed_workspace(client)
            r1 = await client.post(f"/v1/api/workspaces/{slug}/archive")
            assert r1.status_code == 200, r1.text
            r2 = await client.post(f"/v1/api/workspaces/{slug}/unarchive")
            assert r2.status_code == 200, (
                f"unarchive returned {r2.status_code} body={r2.text}"
            )
            await _assert_audit_row("workspace.unarchived", user_sub)


async def test_export_endpoint_resolves_identity_under_real_authmiddleware(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        user_sub, plaintext = await _seed_api_key_post_lifespan()
        async with _make_client(app, plaintext) as client:
            await _require_healthy(client)
            slug = await _seed_workspace(client)
            resp = await client.get(f"/v1/api/workspaces/{slug}/export")
            assert resp.status_code == 200, (
                f"export returned {resp.status_code} body={resp.text}"
            )
            await _assert_audit_row("workspace.exported", user_sub)


async def test_import_endpoint_resolves_identity_under_real_authmiddleware(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        user_sub, plaintext = await _seed_api_key_post_lifespan()
        async with _make_client(app, plaintext) as client:
            await _require_healthy(client)
            zb = _make_min_zip()
            slug = f"g2-imp-{uuid4().hex[:8]}"
            resp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": slug},
                files={"file": ("a.zip", zb, "application/zip")},
            )
            assert resp.status_code == 201, (
                f"import returned {resp.status_code} body={resp.text}"
            )
            await _assert_audit_row("workspace.imported", user_sub)
