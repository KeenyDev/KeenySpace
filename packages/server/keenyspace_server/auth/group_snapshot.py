"""Persist the groups claim of OIDC tokens as the owner's snapshot in `users`.

API keys carry no IdP claims, so ApiKeyService authorizes a key with the groups
its owner's newest OIDC token asserted. A snapshot is stamped with the token's
`iat` and only ever replaced by a strictly newer assertion, so a token issued
before a group change (or before an admin revoke-all, which writes an empty
snapshot) cannot roll the snapshot back while it is still valid. Writes of an
unchanged group set are debounced per-process like api_keys.last_used_at.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from keenyspace_server.auth.api_keys import DbFactory, snapshot_groups
from keenyspace_server.auth.user import User
from keenyspace_server.db.models import User as UserRow

log = structlog.get_logger(__name__)

_DISPLAY_NAME_MAX = 256
KNOWN_SNAPSHOTS_MAX_ENTRIES = 4096


@dataclass(frozen=True, slots=True)
class GroupSnapshot:
    groups: tuple[str, ...]
    seen_at: datetime


class GroupSnapshotStore:
    def __init__(self, *, db_factory: DbFactory, debounce_seconds: int = 300) -> None:
        self._db_factory = db_factory
        self._debounce = debounce_seconds
        # sub -> (newest snapshot known to be stored, monotonic time it was synced)
        self._known: OrderedDict[str, tuple[GroupSnapshot, float]] = OrderedDict()

    async def observe(self, user: User) -> tuple[GroupSnapshot, bool]:
        """Record the groups an OIDC token asserts, unless a newer snapshot exists.

        Returns the snapshot now in effect for the user and whether a row was
        written. `user.groups_seen_at` must be the token's iat.
        """
        if user.groups_seen_at is None:
            raise ValueError("observe() needs a token that carried a groups claim")
        asserted = GroupSnapshot(tuple(sorted(set(user.groups))), user.groups_seen_at)
        now_mono = time.monotonic()
        known = self._known.get(user.sub)
        if known is not None:
            stored, synced_at = known
            if asserted.seen_at <= stored.seen_at:
                return stored, False
            if asserted.groups == stored.groups and now_mono - synced_at < self._debounce:
                return stored, False
        insert = pg_insert(UserRow).values(
            sub=user.sub,
            display_name=user.display_name[:_DISPLAY_NAME_MAX],
            email=None,
            source="oidc",
            created_at=asserted.seen_at,
            groups=list(asserted.groups),
            groups_seen_at=asserted.seen_at,
        )
        upsert = insert.on_conflict_do_update(
            index_elements=[UserRow.sub],
            set_={
                "groups": insert.excluded.groups,
                "groups_seen_at": insert.excluded.groups_seen_at,
            },
            where=or_(
                UserRow.groups_seen_at.is_(None),
                UserRow.groups_seen_at < insert.excluded.groups_seen_at,
            ),
        ).returning(UserRow.sub)
        async with self._db_factory() as session:
            wrote = (await session.execute(upsert)).first() is not None
            if wrote:
                current = asserted
            else:
                row = (
                    await session.execute(
                        select(UserRow.groups, UserRow.groups_seen_at).where(
                            UserRow.sub == user.sub
                        )
                    )
                ).one()
                if row.groups_seen_at is None:
                    raise RuntimeError(f"group snapshot upsert for {user.sub!r} was skipped")
                current = GroupSnapshot(snapshot_groups(row.groups) or (), row.groups_seen_at)
            await session.commit()
        if wrote and known is not None and known[0].groups != asserted.groups:
            log.info("auth.group_snapshot.changed", sub=user.sub, group_count=len(asserted.groups))
        self._remember(user.sub, current, now_mono)
        return current, wrote

    def forget(self, user_sub: str) -> None:
        """Drop what this process knows about a user's snapshot (it changed elsewhere)."""
        self._known.pop(user_sub, None)

    def forget_all(self) -> None:
        """Force the next OIDC request of every user to consult the database."""
        self._known.clear()

    def _remember(self, user_sub: str, snapshot: GroupSnapshot, synced_at: float) -> None:
        self._known[user_sub] = (snapshot, synced_at)
        self._known.move_to_end(user_sub)
        while len(self._known) > KNOWN_SNAPSHOTS_MAX_ENTRIES:
            self._known.popitem(last=False)
