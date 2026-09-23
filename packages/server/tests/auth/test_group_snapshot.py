"""GroupSnapshotStore: OIDC groups claim -> users.groups / users.groups_seen_at.

A snapshot is stamped with the asserting token's iat and only a strictly newer
token may replace it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from keenyspace_server.auth.api_keys import ApiKeyService
from keenyspace_server.auth.group_snapshot import GroupSnapshot, GroupSnapshotStore
from keenyspace_server.auth.user import User
from keenyspace_server.db.session import get_db_session
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("_engine_lifespan_ctx")

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _token_user(sub: str, groups: list[str], iat: datetime) -> User:
    return User(
        sub=sub,
        _display_name="Alice",
        source="oidc",
        groups=groups,
        groups_seen_at=iat,
        issued_at=iat,
    )


def _store(**kwargs: Any) -> GroupSnapshotStore:
    return GroupSnapshotStore(db_factory=get_db_session, **kwargs)


async def _row(sub: str) -> Any:
    async with get_db_session() as session:
        return (
            await session.execute(
                text(
                    "SELECT display_name, email, source, groups, groups_seen_at "
                    "FROM users WHERE sub = :s"
                ),
                {"s": sub},
            )
        ).one()


async def test_first_observation_creates_user_with_snapshot_stamped_with_iat() -> None:
    snapshot, wrote = await _store().observe(_token_user("u-new", ["b", "a", "a"], T0))

    row = await _row("u-new")
    assert (wrote, snapshot) == (True, GroupSnapshot(("a", "b"), T0))
    assert (row.display_name, row.source, row.groups, row.groups_seen_at) == (
        "Alice",
        "oidc",
        ["a", "b"],
        T0,
    )


async def test_observation_keeps_profile_fields_of_existing_user() -> None:
    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES ('u-old', 'Browser Name', 'old@example.com', 'oidc', now())"
            )
        )
        await session.commit()

    await _store().observe(_token_user("u-old", ["g"], T0))

    row = await _row("u-old")
    assert (row.display_name, row.email, row.groups) == ("Browser Name", "old@example.com", ["g"])


async def test_older_token_cannot_overwrite_newer_snapshot() -> None:
    await _store().observe(_token_user("u-roll", [], T0 + timedelta(minutes=10)))

    snapshot, wrote = await _store().observe(_token_user("u-roll", ["keenyspace-admins"], T0))

    assert wrote is False
    assert snapshot == GroupSnapshot((), T0 + timedelta(minutes=10))
    row = await _row("u-roll")
    assert (row.groups, row.groups_seen_at) == ([], T0 + timedelta(minutes=10))


async def test_older_token_is_refused_by_the_process_that_saw_the_newer_one() -> None:
    store = _store()
    await store.observe(_token_user("u-mem", [], T0 + timedelta(minutes=10)))

    snapshot, wrote = await store.observe(_token_user("u-mem", ["keenyspace-admins"], T0))

    assert (wrote, snapshot.groups) == (False, ())


async def test_older_token_cannot_overwrite_revoke_all_tombstone() -> None:
    service = ApiKeyService(pepper="p" * 32, db_factory=get_db_session)
    before_revoke = datetime.now(UTC) - timedelta(minutes=1)
    await _store().observe(_token_user("u-off", ["keenyspace-admins"], before_revoke))

    await service.revoke_all_for_user("u-off", actor_sub="admin")
    snapshot, wrote = await _store().observe(
        _token_user("u-off", ["keenyspace-admins"], before_revoke)
    )

    assert (wrote, snapshot.groups) == (False, ())
    assert (await _row("u-off")).groups == []


async def test_newer_token_replaces_snapshot() -> None:
    store = _store()
    await store.observe(_token_user("u-chg", ["keenyspace-admins"], T0))

    snapshot, wrote = await store.observe(_token_user("u-chg", [], T0 + timedelta(seconds=1)))

    assert (wrote, snapshot.groups) == (True, ())
    assert (await _row("u-chg")).groups == []


async def test_unchanged_groups_from_newer_token_are_debounced() -> None:
    store = _store(debounce_seconds=300)
    await store.observe(_token_user("u-deb", ["g"], T0))

    _, wrote = await store.observe(_token_user("u-deb", ["g"], T0 + timedelta(minutes=1)))

    assert wrote is False
    assert (await _row("u-deb")).groups_seen_at == T0


async def test_forget_forces_the_next_write() -> None:
    store = _store(debounce_seconds=300)
    await store.observe(_token_user("u-fgt", ["g"], T0))

    store.forget("u-fgt")

    _, wrote = await store.observe(_token_user("u-fgt", ["g"], T0 + timedelta(minutes=1)))
    assert wrote is True


async def test_observe_requires_a_groups_claim() -> None:
    user = User(sub="u", _display_name="u", source="oidc", issued_at=T0)

    with pytest.raises(ValueError, match="groups claim"):
        await _store().observe(user)
