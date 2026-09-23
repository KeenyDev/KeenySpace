"""Admin authorization (auth.admin_group) and API-key group snapshots over HTTP.

Admin endpoints require the admin group: from the token claim for OIDC
principals, from the owner's group snapshot for API keys. OIDC requests carrying
a groups claim refresh that snapshot.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from keenyspace_server.db.session import get_db_session
from sqlalchemy import text

ADMIN_GROUP = "keenyspace-admins"


def _http(app: Any, token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _snapshot(sub: str) -> tuple[Any, Any] | None:
    async with get_db_session() as session:
        row = (
            await session.execute(
                text("SELECT groups, groups_seen_at FROM users WHERE sub = :s"), {"s": sub}
            )
        ).one_or_none()
    return None if row is None else (row.groups, row.groups_seen_at)


class TestApiKeyPrincipals:
    async def test_key_without_admin_group_is_forbidden_on_every_admin_route(
        self, client: AsyncClient
    ) -> None:
        backup = await client.post("/v1/admin/backup")
        restore = await client.post(
            "/v1/admin/restore",
            files={"file": ("b.tar.gz", b"not-a-tarball", "application/gzip")},
        )
        revoke = await client.post("/v1/admin/api-keys/revoke-all", json={"sub": "someone"})

        assert [backup.status_code, restore.status_code, revoke.status_code] == [403, 403, 403]
        assert backup.json() == {"detail": "forbidden"}

    async def test_admin_key_revokes_every_key_of_a_user(
        self, admin_client: AsyncClient, api_key_user: tuple[str, str], app: Any
    ) -> None:
        target_sub, target_key = api_key_user
        async with _http(app, target_key) as target:
            assert (await target.get("/v1/api/auth/api-keys")).status_code == 200

            resp = await admin_client.post(
                "/v1/admin/api-keys/revoke-all", json={"sub": target_sub}
            )

            assert resp.status_code == 200, resp.text
            assert resp.json() == {"sub": target_sub, "revoked": 1}
            assert (await target.get("/v1/api/auth/api-keys")).status_code == 401

    @pytest.mark.parametrize("sub", ["", "x" * 257], ids=["empty", "too-long"])
    async def test_revoke_all_validates_sub(self, admin_client: AsyncClient, sub: str) -> None:
        resp = await admin_client.post("/v1/admin/api-keys/revoke-all", json={"sub": sub})

        assert resp.status_code == 422

    async def test_empty_admin_group_disables_admin_api(
        self, admin_client: AsyncClient, app: Any
    ) -> None:
        app.state.settings.auth.admin_group = ""

        resp = await admin_client.post("/v1/admin/api-keys/revoke-all", json={"sub": "someone"})

        assert resp.status_code == 403

    async def test_expired_key_is_unauthenticated(self, app: Any, seed_api_key: Any) -> None:
        _, key = await seed_api_key(
            groups=["keenyspace-users", ADMIN_GROUP],
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        async with _http(app, key) as c:
            resp = await c.post("/v1/admin/api-keys/revoke-all", json={"sub": "someone"})

        assert resp.status_code == 401


class TestMintExpiry:
    """Minting through the OIDC-principal stub (`api_key_client`)."""

    async def test_mint_with_expiry_reports_it(self, api_key_client: AsyncClient) -> None:
        before = datetime.now(UTC)

        resp = await api_key_client.post(
            "/v1/api/auth/api-keys", json={"name": "short", "expires_in_days": 30}
        )

        assert resp.status_code == 201, resp.text
        expires_at = datetime.fromisoformat(resp.json()["expires_at"])
        assert before + timedelta(days=30) <= expires_at <= datetime.now(UTC) + timedelta(days=30)
        listed = (await api_key_client.get("/v1/api/auth/api-keys")).json()
        assert {item["id"]: item["expires_at"] for item in listed}[resp.json()["id"]] is not None

    async def test_mint_without_expiry_never_expires(self, api_key_client: AsyncClient) -> None:
        resp = await api_key_client.post("/v1/api/auth/api-keys", json={"name": "forever"})

        assert resp.status_code == 201
        assert resp.json()["expires_at"] is None

    @pytest.mark.parametrize("days", [0, 3651, -1], ids=["zero", "over-ten-years", "negative"])
    async def test_mint_rejects_out_of_range_expiry(
        self, api_key_client: AsyncClient, days: int
    ) -> None:
        resp = await api_key_client.post(
            "/v1/api/auth/api-keys", json={"name": "bad", "expires_in_days": days}
        )

        assert resp.status_code == 422


class TestKeysCannotMintKeys:
    async def test_expiring_key_cannot_mint_a_successor(self, app: Any, seed_api_key: Any) -> None:
        sub, key = await seed_api_key(
            groups=["keenyspace-users", ADMIN_GROUP],
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        async with _http(app, key) as c:
            resp = await c.post("/v1/api/auth/api-keys", json={"name": "successor"})
            listed = (await c.get("/v1/api/auth/api-keys")).json()

        assert resp.status_code == 403
        assert "OIDC" in resp.json()["detail"]
        assert len(listed) == 1, f"no key may have been minted for {sub}"

    async def test_key_can_still_list_and_revoke_its_owners_keys(self, client: AsyncClient) -> None:
        (item,) = (await client.get("/v1/api/auth/api-keys")).json()

        assert (await client.delete(f"/v1/api/auth/api-keys/{item['id']}")).status_code == 204


@pytest.fixture
def _admin_api_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEENYSPACE_ADMIN_API_ENABLED", "1")


@pytest.fixture
def _users_group_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEENYSPACE_AUTH__REQUIRED_GROUP", "keenyspace-users")


@pytest_asyncio.fixture
async def oidc_app(_admin_api_enabled: None, app_with_mocked_authentik: Any) -> AsyncIterator[Any]:
    yield app_with_mocked_authentik


def _token(
    provider: dict[str, Any], sub: str, groups: list[str] | None, *, age_seconds: int = 0
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": provider["issuer"],
        "aud": "keenyspace-test",
        "scope": "openid profile email groups",
        "sub": sub,
        "iat": now - age_seconds,
        "exp": now + 3600,
    }
    if groups is not None:
        claims["groups"] = groups
    return str(provider["sign_jwt"](claims))


class TestOidcPrincipals:
    async def test_admin_group_claim_grants_admin(self, oidc_app: Any) -> None:
        app, provider = oidc_app
        async with _http(app, _token(provider, "u-admin", [ADMIN_GROUP])) as c:
            resp = await c.post("/v1/admin/api-keys/revoke-all", json={"sub": "u-other"})

        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize(
        "groups", [["keenyspace-users"], None], ids=["not-in-group", "no-groups-claim"]
    )
    async def test_token_without_admin_group_is_forbidden(
        self, oidc_app: Any, groups: list[str] | None
    ) -> None:
        app, provider = oidc_app
        async with _http(app, _token(provider, "u-user", groups)) as c:
            resp = await c.post("/v1/admin/api-keys/revoke-all", json={"sub": "u-other"})

        assert resp.status_code == 403

    async def test_groups_claim_is_recorded_as_snapshot(self, oidc_app: Any) -> None:
        app, provider = oidc_app
        async with _http(app, _token(provider, "u-snap", ["keenyspace-users"])) as c:
            assert (await c.get("/v1/api/auth/api-keys")).status_code == 200

        snapshot = await _snapshot("u-snap")
        assert snapshot is not None
        assert snapshot[0] == ["keenyspace-users"]
        assert snapshot[1] is not None

    async def test_token_without_groups_claim_leaves_snapshot_untouched(
        self, oidc_app: Any
    ) -> None:
        app, provider = oidc_app
        async with _http(app, _token(provider, "u-noclaim", None)) as c:
            assert (await c.get("/v1/api/auth/api-keys")).status_code == 200

        assert await _snapshot("u-noclaim") is None

    async def test_key_minted_by_admin_session_can_call_admin_api(self, oidc_app: Any) -> None:
        app, provider = oidc_app
        async with _http(app, _token(provider, "u-drill", ["keenyspace-users", ADMIN_GROUP])) as c:
            minted = await c.post("/v1/api/auth/api-keys", json={"name": "drill"})
        assert minted.status_code == 201, minted.text

        async with _http(app, minted.json()["key"]) as key_client:
            resp = await key_client.post("/v1/admin/api-keys/revoke-all", json={"sub": "u-x"})

        assert resp.status_code == 200, resp.text

    async def test_group_removal_reaches_cached_key_immediately(self, oidc_app: Any) -> None:
        app, provider = oidc_app
        async with _http(app, _token(provider, "u-leaver", [ADMIN_GROUP], age_seconds=60)) as c:
            key = (await c.post("/v1/api/auth/api-keys", json={"name": "k"})).json()["key"]
        async with _http(app, key) as key_client:
            ok = await key_client.post("/v1/admin/api-keys/revoke-all", json={"sub": "u-x"})
            assert ok.status_code == 200, "precondition: key is admin and now cached"

            async with _http(app, _token(provider, "u-leaver", [])) as c:
                assert (await c.get("/v1/api/auth/api-keys")).status_code == 200

            denied = await key_client.post("/v1/admin/api-keys/revoke-all", json={"sub": "u-x"})

        assert denied.status_code == 403


class TestRequiredGroupForKeys:
    async def test_key_is_admitted_only_with_group_in_owner_snapshot(
        self, _users_group_required: None, app_with_mocked_authentik: Any
    ) -> None:
        app, provider = app_with_mocked_authentik
        async with _http(
            app, _token(provider, "u-member", ["keenyspace-users"], age_seconds=60)
        ) as c:
            key = (await c.post("/v1/api/auth/api-keys", json={"name": "k"})).json()["key"]

        async with _http(app, key) as key_client:
            assert (await key_client.get("/v1/api/auth/api-keys")).status_code == 200

            async with _http(app, _token(provider, "u-member", ["other"])) as c:
                assert (await c.get("/v1/api/auth/api-keys")).status_code == 401

            assert (await key_client.get("/v1/api/auth/api-keys")).status_code == 401

    async def test_key_of_owner_without_snapshot_is_refused(
        self, _users_group_required: None, app_with_mocked_authentik: Any
    ) -> None:
        app, _ = app_with_mocked_authentik
        minted = await app.state.api_key_service.mint(
            user_sub="u-never-logged-in", name="k", credential_issued_at=datetime.now(UTC)
        )
        key = minted["key"]

        async with _http(app, key) as c:
            resp = await c.get("/v1/api/auth/api-keys")

        assert resp.status_code == 401


class TestSnapshotRollback:
    async def test_older_admin_token_is_demoted_by_newer_snapshot(self, oidc_app: Any) -> None:
        app, provider = oidc_app
        old_admin_token = _token(provider, "u-demoted", [ADMIN_GROUP], age_seconds=600)
        async with _http(app, _token(provider, "u-demoted", ["keenyspace-users"])) as c:
            assert (await c.get("/v1/api/auth/api-keys")).status_code == 200

        async with _http(app, old_admin_token) as c:
            resp = await c.post("/v1/admin/api-keys/revoke-all", json={"sub": "u-x"})

        assert resp.status_code == 403
        snapshot = await _snapshot("u-demoted")
        assert snapshot is not None
        assert snapshot[0] == ["keenyspace-users"]

    async def test_pre_revoke_token_cannot_mint_after_revoke_all(self, oidc_app: Any) -> None:
        app, provider = oidc_app
        stale = _token(provider, "u-offboarded", ["keenyspace-users"], age_seconds=120)
        async with _http(app, stale) as c:
            assert (await c.get("/v1/api/auth/api-keys")).status_code == 200
        async with _http(app, _token(provider, "u-admin", [ADMIN_GROUP])) as admin:
            revoked = await admin.post(
                "/v1/admin/api-keys/revoke-all", json={"sub": "u-offboarded"}
            )
            assert revoked.status_code == 200, revoked.text

        async with _http(app, stale) as c:
            minted = await c.post("/v1/api/auth/api-keys", json={"name": "persist"})
            listed = (await c.get("/v1/api/auth/api-keys")).json()

        assert minted.status_code == 403
        assert listed == []
        snapshot = await _snapshot("u-offboarded")
        assert snapshot is not None
        assert snapshot[0] == []
