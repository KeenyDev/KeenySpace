"""GroupSnapshotStore: OIDC groups claim -> users.groups / users.groups_seen_at."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from keenyspace_server.auth.group_snapshot import GroupSnapshotStore
from keenyspace_server.auth.user import User
from keenyspace_server.db.session import get_db_session
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("_engine_lifespan_ctx")


def _oidc_user(sub: str, groups: list[str], seen_at: datetime | None = None) -> User:
    return User(
        sub=sub,
        _display_name="Alice",
        source="oidc",
        groups=groups,
        groups_seen_at=seen_at or datetime.now(UTC),
    )


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


async def test_first_record_creates_user_with_snapshot() -> None:
    seen = datetime.now(UTC)

    wrote = await GroupSnapshotStore(db_factory=get_db_session).record(
        _oidc_user("u-new", ["b", "a", "a"], seen)
    )

    row = await _row("u-new")
    assert wrote is True
    assert (row.display_name, row.source, row.groups) == ("Alice", "oidc", ["a", "b"])
    assert row.groups_seen_at == seen


async def test_record_keeps_profile_fields_of_existing_user() -> None:
    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES ('u-old', 'Browser Name', 'old@example.com', 'oidc', now())"
            )
        )
        await session.commit()

    await GroupSnapshotStore(db_factory=get_db_session).record(_oidc_user("u-old", ["g"]))

    row = await _row("u-old")
    assert (row.display_name, row.email, row.groups) == ("Browser Name", "old@example.com", ["g"])


async def test_unchanged_groups_are_debounced() -> None:
    store = GroupSnapshotStore(db_factory=get_db_session, debounce_seconds=300)
    first_seen = datetime.now(UTC) - timedelta(minutes=1)
    await store.record(_oidc_user("u-deb", ["g"], first_seen))

    wrote = await store.record(_oidc_user("u-deb", ["g"]))

    assert wrote is False
    assert (await _row("u-deb")).groups_seen_at == first_seen


async def test_changed_groups_are_written_immediately() -> None:
    store = GroupSnapshotStore(db_factory=get_db_session, debounce_seconds=300)
    await store.record(_oidc_user("u-chg", ["keenyspace-admins"]))

    wrote = await store.record(_oidc_user("u-chg", []))

    assert wrote is True
    assert (await _row("u-chg")).groups == []


async def test_forget_all_forces_the_next_write() -> None:
    store = GroupSnapshotStore(db_factory=get_db_session, debounce_seconds=300)
    await store.record(_oidc_user("u-fgt", ["g"]))

    store.forget_all()

    assert await store.record(_oidc_user("u-fgt", ["g"])) is True
