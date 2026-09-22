"""Persist the groups claim of OIDC tokens as the owner's snapshot in `users`.

API keys carry no IdP claims, so ApiKeyService authorizes a key with the groups
its owner presented in their most recent OIDC token. Writes are debounced
per-process like api_keys.last_used_at, except that a changed group set is
written at once so a removal in the IdP reaches the owner's keys on their next
OIDC request.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from keenyspace_server.auth.api_keys import DbFactory
from keenyspace_server.auth.user import User
from keenyspace_server.db.models import User as UserRow

log = structlog.get_logger(__name__)

_DISPLAY_NAME_MAX = 256


class GroupSnapshotStore:
    def __init__(self, *, db_factory: DbFactory, debounce_seconds: int = 300) -> None:
        self._db_factory = db_factory
        self._debounce = debounce_seconds
        self._last_writes: dict[str, tuple[float, tuple[str, ...]]] = {}

    async def record(self, user: User) -> bool:
        """Upsert the snapshot for an OIDC principal; True when a row was written."""
        groups = tuple(sorted(set(user.groups)))
        last = self._last_writes.get(user.sub)
        now_mono = time.monotonic()
        if last is not None and last[1] == groups and now_mono - last[0] < self._debounce:
            return False
        seen_at = user.groups_seen_at or datetime.now(UTC)
        stmt = pg_insert(UserRow).values(
            sub=user.sub,
            display_name=user.display_name[:_DISPLAY_NAME_MAX],
            email=None,
            source="oidc",
            created_at=seen_at,
            groups=list(groups),
            groups_seen_at=seen_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserRow.sub],
            set_={"groups": stmt.excluded.groups, "groups_seen_at": stmt.excluded.groups_seen_at},
        )
        async with self._db_factory() as session:
            await session.execute(stmt)
            await session.commit()
        self._last_writes[user.sub] = (now_mono, groups)
        if last is not None and last[1] != groups:
            log.info("auth.group_snapshot.changed", sub=user.sub, group_count=len(groups))
        return True

    def forget_all(self) -> None:
        """Force the next OIDC request of every user to rewrite its snapshot."""
        self._last_writes.clear()
