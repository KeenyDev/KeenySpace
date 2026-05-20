"""AUTH-03/09 router-level integration tests (P-9 ASGITransport).

Tests run via `api_key_client` fixture которая инжектит test-only AuthenticationBackend
(заменяет CompositeAuthBackend на authenticated stub). Wave 1 первоначально использовал
этот fixture как middleware-bypass поверх переходного backend; Wave 2 cutover оставил
fixture для router-level изоляции (быстрая обратная связь без полной chain).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest


@pytest.mark.asyncio
async def test_post_mints_plaintext_once(api_key_client) -> None:
    resp = await api_key_client.post("/v1/api/auth/api-keys", json={"name": "dev"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["key"].startswith("ks_live_")
    assert len(body["key"]) == len("ks_live_") + 43
    UUID(body["id"])
    assert body["name"] == "dev"
    assert body["key_prefix"] == "ks_live_"
    assert body["last4"] == body["key"][-4:]


@pytest.mark.asyncio
async def test_post_empty_name_returns_422(api_key_client) -> None:
    resp = await api_key_client.post("/v1/api/auth/api-keys", json={"name": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_oversized_name_returns_422(api_key_client) -> None:
    resp = await api_key_client.post("/v1/api/auth/api-keys", json={"name": "x" * 129})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_lists_without_plaintext(api_key_client) -> None:
    minted = (await api_key_client.post("/v1/api/auth/api-keys", json={"name": "k1"})).json()
    resp = await api_key_client.get("/v1/api/auth/api-keys")
    assert resp.status_code == 200
    items = resp.json()
    assert any(it["id"] == minted["id"] for it in items)
    for it in items:
        assert "key" not in it
        assert "hash" not in it
        assert "lookup_hash" not in it


@pytest.mark.asyncio
async def test_delete_revokes_key(api_key_client) -> None:
    minted = (await api_key_client.post("/v1/api/auth/api-keys", json={"name": "k2"})).json()
    resp = await api_key_client.delete(f"/v1/api/auth/api-keys/{minted['id']}")
    assert resp.status_code == 204
    items = (await api_key_client.get("/v1/api/auth/api-keys")).json()
    target = next(it for it in items if it["id"] == minted["id"])
    assert target["revoked_at"] is not None
    resp2 = await api_key_client.delete(f"/v1/api/auth/api-keys/{minted['id']}")
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_delete_random_id_returns_404(api_key_client) -> None:
    resp = await api_key_client.delete(f"/v1/api/auth/api-keys/{uuid4()}")
    assert resp.status_code == 404


def test_admin_stub_removed(app) -> None:
    """F-02: /v1/admin/api-keys убран из main.py (T-3-15)."""
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/v1/admin/api-keys" not in paths


@pytest.mark.asyncio
async def test_audit_log_minted_no_plaintext(api_key_client, pg_url) -> None:
    """T-3-10: audit_log payload не содержит plaintext."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    minted = (await api_key_client.post("/v1/api/auth/api-keys", json={"name": "audit"})).json()
    plaintext = minted["key"]
    body = plaintext[len("ks_live_") :]
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        r = await conn.execute(
            sa.text("SELECT payload FROM audit_log WHERE action='auth.api_key.minted'")
        )
        rows = [row[0] for row in r]
    await engine.dispose()
    assert rows, "expected at least one audit_log row"
    for p in rows:
        s = str(p)
        assert plaintext not in s
        assert body not in s


@pytest.mark.asyncio
async def test_audit_log_revoked_payload_shape(api_key_client, pg_url) -> None:
    """T-3-16: revoke audit payload содержит только key_id."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    minted = (await api_key_client.post("/v1/api/auth/api-keys", json={"name": "r"})).json()
    resp = await api_key_client.delete(f"/v1/api/auth/api-keys/{minted['id']}")
    assert resp.status_code == 204
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        r = await conn.execute(
            sa.text("SELECT payload FROM audit_log WHERE action='auth.api_key.revoked'")
        )
        payloads = [row[0] for row in r]
    await engine.dispose()
    assert any(p.get("key_id") == minted["id"] for p in payloads)
