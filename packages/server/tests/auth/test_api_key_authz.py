"""ApiKeyService authorization: owner group snapshot, expiry, verification cache, audit atomicity.

Runs the real service against the test database (engine_lifespan from the
`seed_api_key` fixture); keys are seeded directly with real argon2 hashes.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import structlog.testing
from keenyspace_server.auth import api_keys as api_keys_module
from keenyspace_server.auth.api_keys import (
    ApiKeyService,
    StaleCredentialError,
    _compute_lookup_hash,
)
from keenyspace_server.db.session import get_db_session
from sqlalchemy import text

PEPPER = "test-pepper-32chars-padded-here!"
ADMIN_GROUPS = ["keenyspace-users", "keenyspace-admins"]


def _now() -> datetime:
    return datetime.now(UTC)


def _service(**kwargs: Any) -> ApiKeyService:
    return ApiKeyService(pepper=PEPPER, db_factory=get_db_session, **kwargs)


async def _revoke_in_db_only(user_sub: str) -> None:
    async with get_db_session() as session:
        await session.execute(
            text("UPDATE api_keys SET revoked_at = now() WHERE user_sub = :sub"), {"sub": user_sub}
        )
        await session.commit()


async def _key_rows(user_sub: str) -> list[Any]:
    async with get_db_session() as session:
        result = await session.execute(
            text("SELECT id, revoked_at FROM api_keys WHERE user_sub = :sub"), {"sub": user_sub}
        )
        return list(result)


async def _audit_rows(action: str) -> list[Any]:
    async with get_db_session() as session:
        result = await session.execute(
            text("SELECT actor_sub, payload FROM audit_log WHERE action = :a"), {"a": action}
        )
        return list(result)


async def test_verify_returns_owner_snapshot_groups(seed_api_key) -> None:
    sub, key = await seed_api_key(groups=ADMIN_GROUPS)

    user = await _service().verify(key)

    assert user is not None
    assert (user.sub, user.source, user.groups) == (sub, "api_key", ADMIN_GROUPS)
    assert user.groups_seen_at is not None


async def test_verify_without_snapshot_returns_groupless_user(seed_api_key) -> None:
    _, key = await seed_api_key(groups=None)

    user = await _service().verify(key)

    assert user is not None
    assert user.groups == []
    assert user.groups_seen_at is None


async def test_expired_key_is_rejected(seed_api_key) -> None:
    _, key = await seed_api_key(
        groups=ADMIN_GROUPS, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    with structlog.testing.capture_logs() as logs:
        assert await _service().verify(key) is None

    assert any(e["event"] == "auth.api_key.denied" and e["reason"] == "expired" for e in logs)


async def test_key_before_expiry_is_accepted(seed_api_key) -> None:
    _, key = await seed_api_key(groups=None, expires_at=datetime.now(UTC) + timedelta(days=1))

    assert await _service().verify(key) is not None


async def test_expiry_is_enforced_on_cache_hits(
    seed_api_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub, key = await seed_api_key(groups=None, expires_at=datetime.now(UTC) + timedelta(days=1))
    service = _service()
    assert await service.verify(key) is not None
    await _revoke_in_db_only(sub)
    assert await service.verify(key) is not None, "precondition: served from the cache"

    class _TwoDaysLater(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime.now(tz) + timedelta(days=2)

    monkeypatch.setattr(api_keys_module, "datetime", _TwoDaysLater)

    assert await service.verify(key) is None


@pytest.mark.parametrize(
    ("seen_ago", "admitted"),
    [
        pytest.param(timedelta(hours=1), True, id="fresh-snapshot"),
        pytest.param(timedelta(days=2), False, id="stale-snapshot"),
    ],
)
async def test_snapshot_max_age(seed_api_key, seen_ago: timedelta, admitted: bool) -> None:
    _, key = await seed_api_key(groups=ADMIN_GROUPS, groups_seen_at=datetime.now(UTC) - seen_ago)

    user = await _service(group_snapshot_max_age=timedelta(days=1)).verify(key)

    assert (user is not None) is admitted


async def test_snapshot_max_age_denies_key_whose_owner_never_logged_in(seed_api_key) -> None:
    _, key = await seed_api_key(groups=None)

    with structlog.testing.capture_logs() as logs:
        user = await _service(group_snapshot_max_age=timedelta(days=1)).verify(key)

    assert user is None
    assert any(e.get("reason") == "group_snapshot_stale" for e in logs)


async def test_successful_verification_is_cached(seed_api_key) -> None:
    sub, key = await seed_api_key(groups=None)
    service = _service()
    assert await service.verify(key) is not None

    await _revoke_in_db_only(sub)

    assert await service.verify(key) is not None, "second verify must be served from the cache"


async def test_revoke_drops_cached_verification(seed_api_key) -> None:
    sub, key = await seed_api_key(groups=None)
    service = _service()
    assert await service.verify(key) is not None
    (key_id, _), = await _key_rows(sub)

    assert await service.revoke(key_id, sub) is True

    assert await service.verify(key) is None


async def test_revoke_all_drops_cached_verifications(seed_api_key) -> None:
    sub, key = await seed_api_key(groups=None)
    service = _service()
    assert await service.verify(key) is not None

    assert await service.revoke_all_for_user(sub, actor_sub="admin-sub") == 1

    assert await service.verify(key) is None


async def test_failed_lookup_is_not_cached(seed_api_key) -> None:
    sub, key = await seed_api_key(groups=None)
    service = _service()
    body = key[len("ks_live_") :]
    async with get_db_session() as session:
        await session.execute(
            text("UPDATE api_keys SET lookup_hash = 'x' WHERE user_sub = :s"), {"s": sub}
        )
        await session.commit()
    assert await service.verify(key) is None

    async with get_db_session() as session:
        await session.execute(
            text("UPDATE api_keys SET lookup_hash = :lh WHERE user_sub = :s"),
            {"lh": _compute_lookup_hash(body, PEPPER), "s": sub},
        )
        await session.commit()

    assert await service.verify(key) is not None


async def test_hash_mismatch_is_not_cached(seed_api_key) -> None:
    sub, key = await seed_api_key(groups=None)
    service = _service()
    async with get_db_session() as session:
        original = (
            await session.execute(text("SELECT hash FROM api_keys WHERE user_sub = :s"), {"s": sub})
        ).scalar_one()
        await session.execute(
            text("UPDATE api_keys SET hash = :h WHERE user_sub = :s"),
            {"h": api_keys_module._PH.hash("some-other-body"), "s": sub},
        )
        await session.commit()
    assert await service.verify(key) is None

    async with get_db_session() as session:
        await session.execute(
            text("UPDATE api_keys SET hash = :h WHERE user_sub = :s"), {"h": original, "s": sub}
        )
        await session.commit()

    assert await service.verify(key) is not None


async def test_verification_racing_an_invalidation_is_not_cached(
    seed_api_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub, key = await seed_api_key(groups=None)
    service = _service()
    entered = threading.Event()
    release = threading.Event()
    real_hasher = api_keys_module._PH

    class _GatedHasher:
        def verify(self, hash_: str, password: str) -> bool:
            entered.set()
            release.wait(timeout=10)
            return real_hasher.verify(hash_, password)

    monkeypatch.setattr(api_keys_module, "_PH", _GatedHasher())
    in_flight = asyncio.create_task(service.verify(key))
    await asyncio.to_thread(entered.wait, 10)
    service.forget_user(sub)
    release.set()
    assert await in_flight is not None
    monkeypatch.setattr(api_keys_module, "_PH", real_hasher)

    await _revoke_in_db_only(sub)

    assert await service.verify(key) is None, "the in-flight result must not have been cached"


async def test_mint_and_audit_commit_together(
    seed_api_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub, _ = await seed_api_key(groups=None)

    async def _failing_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(api_keys_module, "write_audit", _failing_audit)

    with pytest.raises(RuntimeError, match="audit store unavailable"):
        await _service().mint(user_sub=sub, name="atomic", credential_issued_at=_now())

    assert len(await _key_rows(sub)) == 1, "the minted key must not persist without its audit row"


async def test_revoke_and_audit_commit_together(
    seed_api_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub, key = await seed_api_key(groups=None)
    (key_id, _), = await _key_rows(sub)

    async def _failing_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(api_keys_module, "write_audit", _failing_audit)

    with pytest.raises(RuntimeError, match="audit store unavailable"):
        await _service().revoke(key_id, sub)

    (_, revoked_at), = await _key_rows(sub)
    assert revoked_at is None
    monkeypatch.undo()
    assert await _service().verify(key) is not None


async def test_mint_writes_audit_row_with_expiry(seed_api_key) -> None:
    sub, _ = await seed_api_key(groups=None)
    expires_at = datetime.now(UTC) + timedelta(days=7)

    minted = await _service().mint(
        user_sub=sub, name="exp", credential_issued_at=_now(), expires_at=expires_at
    )

    rows = await _audit_rows("auth.api_key.minted")
    assert [(r.actor_sub, r.payload) for r in rows] == [
        (sub, {"key_id": str(minted["id"]), "name": "exp", "expires_at": expires_at.isoformat()})
    ]


async def test_revoke_all_is_audited_with_target_and_keys(seed_api_key) -> None:
    sub, _ = await seed_api_key(groups=None)
    other = await _service().mint(user_sub=sub, name="second", credential_issued_at=_now())

    revoked = await _service().revoke_all_for_user(sub, actor_sub="admin-sub")

    assert revoked == 2
    assert all(revoked_at is not None for _, revoked_at in await _key_rows(sub))
    (row,) = await _audit_rows("admin.api_keys.revoked_all")
    assert row.actor_sub == "admin-sub"
    assert row.payload["target_sub"] == sub
    assert row.payload["revoked_count"] == 2
    assert str(other["id"]) in row.payload["key_ids"]
    assert all(UUID(k) for k in row.payload["key_ids"])


async def test_revoke_all_without_keys_is_still_audited(seed_api_key) -> None:
    assert await _service().revoke_all_for_user("nobody", actor_sub="admin-sub") == 0

    (row,) = await _audit_rows("admin.api_keys.revoked_all")
    assert row.payload == {"target_sub": "nobody", "revoked_count": 0, "key_ids": []}


async def test_revoke_all_writes_an_empty_snapshot(seed_api_key) -> None:
    sub, _ = await seed_api_key(groups=ADMIN_GROUPS)
    before = _now()

    await _service().revoke_all_for_user(sub, actor_sub="admin-sub")

    async with get_db_session() as session:
        row = (
            await session.execute(
                text("SELECT groups, groups_seen_at FROM users WHERE sub = :s"), {"s": sub}
            )
        ).one()
    assert row.groups == []
    assert row.groups_seen_at >= before


async def test_token_issued_before_revoke_all_cannot_mint(seed_api_key) -> None:
    sub, _ = await seed_api_key(groups=ADMIN_GROUPS, groups_seen_at=_now() - timedelta(hours=1))
    token_iat = _now() - timedelta(minutes=5)
    await _service().revoke_all_for_user(sub, actor_sub="admin-sub")

    with pytest.raises(StaleCredentialError):
        await _service().mint(user_sub=sub, name="persist", credential_issued_at=token_iat)

    assert all(revoked_at is not None for _, revoked_at in await _key_rows(sub))


async def test_token_issued_after_revoke_all_can_mint(seed_api_key) -> None:
    sub, _ = await seed_api_key(groups=ADMIN_GROUPS)
    await _service().revoke_all_for_user(sub, actor_sub="admin-sub")

    minted = await _service().mint(
        user_sub=sub, name="fresh", credential_issued_at=_now() + timedelta(seconds=1)
    )

    user = await _service().verify(minted["key"])
    assert user is not None
    assert user.groups == [], "the new key inherits the empty snapshot until a newer login"


async def test_mint_for_user_without_snapshot_is_allowed(seed_api_key) -> None:
    sub, _ = await seed_api_key(groups=None)

    minted = await _service().mint(user_sub=sub, name="k", credential_issued_at=_now())

    assert minted["key"].startswith("ks_live_")


async def test_last_used_tracking_is_bounded(
    seed_api_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_keys_module, "LAST_USED_TRACKED_MAX_ENTRIES", 1)
    service = _service()
    _, first = await seed_api_key(groups=None)
    _, second = await seed_api_key(groups=None)

    assert await service.verify(first) is not None
    assert await service.verify(second) is not None

    assert len(service._last_used_writes) == 1
