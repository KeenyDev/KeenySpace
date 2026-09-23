"""API-key CRUD router tests, driven end-to-end through ASGITransport.

The `api_key_client` fixture swaps CompositeAuthBackend for a test-only authenticated
AuthenticationBackend, so these tests isolate the router from the full resolver chain;
the chain itself is covered by integration/test_auth_bypass.py.
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
    """The superseded /v1/admin/api-keys stub is no longer mounted on the app."""
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/v1/admin/api-keys" not in paths


@pytest.mark.asyncio
async def test_audit_log_minted_no_plaintext(api_key_client, pg_url) -> None:
    """The mint audit_log payload contains neither the plaintext key nor its body."""
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
    """The revoke audit row carries the key_id and nothing else.

    Pinning the exact key set, not just the presence of key_id, is what makes this a
    regression test: a future payload that also recorded the prefix, last4 or the key
    material itself would still "name the key_id" and would pass a laxer assertion.
    """
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    minted = (await api_key_client.post("/v1/api/auth/api-keys", json={"name": "r"})).json()
    plaintext = minted["key"]
    resp = await api_key_client.delete(f"/v1/api/auth/api-keys/{minted['id']}")
    assert resp.status_code == 204
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        r = await conn.execute(
            sa.text("SELECT payload FROM audit_log WHERE action='auth.api_key.revoked'")
        )
        payloads = [row[0] for row in r]
    await engine.dispose()

    assert len(payloads) == 1, f"expected exactly one revoke audit row, got {payloads}"
    payload = payloads[0]
    assert set(payload) == {"key_id"}, f"revoke payload grew new fields: {sorted(payload)}"
    assert payload["key_id"] == minted["id"]
    assert plaintext[len("ks_live_") :] not in str(payload)
