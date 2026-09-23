"""ApiKeyService — argon2id for verification plus a sha256+pepper lookup hash.

The lookup hash makes verification an O(1) indexed read; the pepper keeps a
leaked database dump from being attacked with a precomputed rainbow table.
The plaintext key is returned exactly once, from the mint response, and is
never stored. last_used_at writes are debounced in-process, which is only
correct because the server runs single-worker.

A key authenticates as its owner with the owner's group snapshot (the groups
last seen in an OIDC token, see auth/group_snapshot.py), so group gates apply
to keys the same way they apply to OIDC tokens. Successful verifications are
cached in-process for a short TTL because argon2 costs ~37 ms and 64 MiB per
call; revocation drops the cached entries immediately.

Minting and an admin revoke-all serialize on a per-user advisory lock. Revoke-all
also writes an empty group snapshot stamped now, and minting refuses a token
issued before the owner's snapshot, so a token that predates an offboarding can
neither mint a key the revoke-all missed nor one that passes the group gates.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.auth.audit import write_audit
from keenyspace_server.auth.user import User
from keenyspace_server.db.models import ApiKey
from keenyspace_server.db.models import User as UserRow

log = structlog.get_logger(__name__)
_PH = PasswordHasher()

VERIFIED_CACHE_TTL_SECONDS = 60.0
VERIFIED_CACHE_MAX_ENTRIES = 1024
LAST_USED_TRACKED_MAX_ENTRIES = 4096
# First key of the two-int advisory lock taken per user by mint and revoke-all.
_USER_KEYS_LOCK_NAMESPACE = 0x6B730001


class StaleCredentialError(Exception):
    """The token predates the owner's newest group snapshot, so it may not mint keys."""


def _generate_key_body() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _full_key(body: str) -> str:
    return f"ks_live_{body}"


def _compute_lookup_hash(body: str, pepper: str) -> str:
    return hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()


def snapshot_groups(raw: object) -> tuple[str, ...] | None:
    """Stored users.groups as a tuple of names; None when no snapshot exists."""
    if not isinstance(raw, list):
        return None
    return tuple(g for g in raw if isinstance(g, str))


DbFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class _VerifiedKey:
    key_id: UUID
    user_sub: str
    expires_at: datetime | None
    groups: tuple[str, ...] | None
    groups_seen_at: datetime | None


class _VerifiedKeyCache:
    """Bounded TTL map lookup_hash -> verified key. Holds successes only."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[str, tuple[float, _VerifiedKey]] = OrderedDict()

    def get(self, lookup_hash: str) -> _VerifiedKey | None:
        item = self._entries.get(lookup_hash)
        if item is None:
            return None
        stored_at, entry = item
        if time.monotonic() - stored_at >= self._ttl:
            del self._entries[lookup_hash]
            return None
        return entry

    def put(self, lookup_hash: str, entry: _VerifiedKey) -> None:
        self._entries[lookup_hash] = (time.monotonic(), entry)
        self._entries.move_to_end(lookup_hash)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def drop_where(self, predicate: Callable[[_VerifiedKey], bool]) -> None:
        for lookup_hash in [h for h, (_, e) in self._entries.items() if predicate(e)]:
            del self._entries[lookup_hash]

    def clear(self) -> None:
        self._entries.clear()


class ApiKeyService:
    def __init__(
        self,
        *,
        pepper: str,
        db_factory: DbFactory,
        debounce_seconds: int = 300,
        group_snapshot_max_age: timedelta | None = None,
    ) -> None:
        self._pepper = pepper
        self._db_factory = db_factory
        self._debounce = debounce_seconds
        self._snapshot_max_age = group_snapshot_max_age
        self._last_used_writes: OrderedDict[UUID, datetime] = OrderedDict()
        self._verified = _VerifiedKeyCache(
            ttl_seconds=VERIFIED_CACHE_TTL_SECONDS,
            max_entries=VERIFIED_CACHE_MAX_ENTRIES,
        )
        # Bumped after every invalidation: a verification that read its row
        # before a concurrent revoke committed must not repopulate the cache.
        self._invalidation_epoch = 0

    async def mint(
        self,
        *,
        user_sub: str,
        name: str,
        credential_issued_at: datetime,
        expires_at: datetime | None = None,
    ) -> Mapping[str, Any]:
        """Create a key for `user_sub` on the strength of an OIDC token issued at
        `credential_issued_at`. Raises StaleCredentialError when the owner's group
        snapshot is newer than that token."""
        body = _generate_key_body()
        plaintext = _full_key(body)
        lookup_hash = _compute_lookup_hash(body, self._pepper)
        argon_hash = await asyncio.to_thread(_PH.hash, body)
        key_id = uuid4()
        now = datetime.now(UTC)
        row = ApiKey(
            id=key_id,
            user_sub=user_sub,
            name=name,
            prefix="ks_live_",
            hash=argon_hash,
            lookup_hash=lookup_hash,
            created_at=now,
            expires_at=expires_at,
        )
        async with self._db_factory() as session:
            await _lock_user_keys(session, user_sub)
            seen_at = (
                await session.execute(select(UserRow.groups_seen_at).where(UserRow.sub == user_sub))
            ).scalar_one_or_none()
            if seen_at is not None and seen_at > credential_issued_at:
                log.warning(
                    "auth.api_key.mint_refused", reason="token_predates_snapshot", user_sub=user_sub
                )
                raise StaleCredentialError(user_sub)
            session.add(row)
            await write_audit(
                session,
                actor_sub=user_sub,
                action="auth.api_key.minted",
                payload={
                    "key_id": str(key_id),
                    "name": name,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                },
            )
            await session.commit()
        log.info(
            "auth.api_key.minted",
            key_id=str(key_id),
            user_sub=user_sub,
            name=name,
        )
        return {
            "id": key_id,
            "name": name,
            "key": plaintext,
            "key_prefix": "ks_live_",
            "last4": body[-4:],
            "created_at": now,
            "expires_at": expires_at,
        }

    async def verify(self, plaintext: str) -> User | None:
        if not plaintext.startswith("ks_live_"):
            return None
        body = plaintext[len("ks_live_") :]
        lookup_hash = _compute_lookup_hash(body, self._pepper)
        entry = self._verified.get(lookup_hash)
        if entry is None:
            entry = await self._load_and_verify(lookup_hash, body)
            if entry is None:
                return None
        now = datetime.now(UTC)
        if entry.expires_at is not None and entry.expires_at <= now:
            log.info("auth.api_key.denied", reason="expired", key_id=str(entry.key_id))
            return None
        if self._snapshot_max_age is not None and (
            entry.groups_seen_at is None or now - entry.groups_seen_at > self._snapshot_max_age
        ):
            log.warning(
                "auth.api_key.denied",
                reason="group_snapshot_stale",
                key_id=str(entry.key_id),
                user_sub=entry.user_sub,
            )
            return None
        await self._maybe_touch_last_used(entry.key_id)
        return User(
            sub=entry.user_sub,
            _display_name=entry.user_sub,
            source="api_key",
            groups=list(entry.groups or ()),
            groups_seen_at=entry.groups_seen_at,
        )

    async def _load_and_verify(self, lookup_hash: str, body: str) -> _VerifiedKey | None:
        epoch = self._invalidation_epoch
        async with self._db_factory() as session:
            result = await session.execute(
                select(
                    ApiKey.id,
                    ApiKey.user_sub,
                    ApiKey.hash,
                    ApiKey.expires_at,
                    UserRow.groups,
                    UserRow.groups_seen_at,
                )
                .outerjoin(UserRow, UserRow.sub == ApiKey.user_sub)
                .where(
                    ApiKey.lookup_hash == lookup_hash,
                    ApiKey.revoked_at.is_(None),
                )
            )
            row = result.one_or_none()
        if row is None:
            return None
        try:
            await asyncio.to_thread(_PH.verify, row.hash, body)
        except VerifyMismatchError, VerificationError, InvalidHashError:
            return None
        groups = snapshot_groups(row.groups)
        entry = _VerifiedKey(
            key_id=row.id,
            user_sub=row.user_sub,
            expires_at=row.expires_at,
            groups=groups,
            groups_seen_at=row.groups_seen_at if groups is not None else None,
        )
        if self._invalidation_epoch == epoch:
            self._verified.put(lookup_hash, entry)
        return entry

    async def list_for_user(self, user_sub: str) -> list[Mapping[str, Any]]:
        async with self._db_factory() as session:
            result = await session.execute(
                select(ApiKey).where(ApiKey.user_sub == user_sub).order_by(ApiKey.created_at.desc())
            )
            rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "key_prefix": r.prefix,
                "last4": "",
                "created_at": r.created_at,
                "last_used_at": r.last_used_at,
                "revoked_at": r.revoked_at,
                "expires_at": r.expires_at,
            }
            for r in rows
        ]

    async def revoke(self, key_id: UUID, user_sub: str) -> bool:
        now = datetime.now(UTC)
        async with self._db_factory() as session:
            cursor: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
                update(ApiKey)
                .where(
                    ApiKey.id == key_id,
                    ApiKey.user_sub == user_sub,
                    ApiKey.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            if cursor.rowcount == 0:
                return False
            await write_audit(
                session,
                actor_sub=user_sub,
                action="auth.api_key.revoked",
                payload={"key_id": str(key_id)},
            )
            await session.commit()
        self._invalidate(lambda e: e.key_id == key_id)
        log.info(
            "auth.api_key.revoked",
            key_id=str(key_id),
            user_sub=user_sub,
        )
        return True

    async def revoke_all_for_user(self, user_sub: str, *, actor_sub: str) -> int:
        """Revoke every key of `user_sub` and replace its group snapshot with an
        empty one stamped now; returns the number of keys revoked."""
        now = datetime.now(UTC)
        tombstone = pg_insert(UserRow).values(
            sub=user_sub,
            display_name=user_sub,
            email=None,
            source="oidc",
            created_at=now,
            groups=[],
            groups_seen_at=now,
        )
        tombstone = tombstone.on_conflict_do_update(
            index_elements=[UserRow.sub],
            set_={"groups": tombstone.excluded.groups, "groups_seen_at": now},
        )
        async with self._db_factory() as session:
            await _lock_user_keys(session, user_sub)
            await session.execute(tombstone)
            result = await session.execute(
                update(ApiKey)
                .where(ApiKey.user_sub == user_sub, ApiKey.revoked_at.is_(None))
                .values(revoked_at=now)
                .returning(ApiKey.id)
            )
            key_ids = [str(key_id) for key_id in result.scalars()]
            await write_audit(
                session,
                actor_sub=actor_sub,
                action="admin.api_keys.revoked_all",
                payload={"target_sub": user_sub, "revoked_count": len(key_ids), "key_ids": key_ids},
            )
            await session.commit()
        self._invalidate(lambda e: e.user_sub == user_sub)
        log.info(
            "admin.api_keys.revoked_all",
            actor_sub=actor_sub,
            target_sub=user_sub,
            revoked_count=len(key_ids),
        )
        return len(key_ids)

    def forget_user(self, user_sub: str) -> None:
        """Drop cached verifications for a user whose group snapshot changed."""
        self._invalidate(lambda e: e.user_sub == user_sub)

    def forget_all(self) -> None:
        """Drop every cached verification, e.g. after api_keys was replaced wholesale."""
        self._invalidation_epoch += 1
        self._verified.clear()

    def _invalidate(self, predicate: Callable[[_VerifiedKey], bool]) -> None:
        self._invalidation_epoch += 1
        self._verified.drop_where(predicate)

    async def _maybe_touch_last_used(self, key_id: UUID) -> None:
        now = datetime.now(UTC)
        last = self._last_used_writes.get(key_id)
        if last is not None and (now - last).total_seconds() < self._debounce:
            return
        self._last_used_writes[key_id] = now
        self._last_used_writes.move_to_end(key_id)
        while len(self._last_used_writes) > LAST_USED_TRACKED_MAX_ENTRIES:
            self._last_used_writes.popitem(last=False)
        async with self._db_factory() as session:
            await session.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=now)
            )
            await session.commit()


async def _lock_user_keys(session: AsyncSession, user_sub: str) -> None:
    await session.execute(
        select(func.pg_advisory_xact_lock(_USER_KEYS_LOCK_NAMESPACE, func.hashtext(user_sub)))
    )
