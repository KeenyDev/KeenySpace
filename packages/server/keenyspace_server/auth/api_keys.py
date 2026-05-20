"""ApiKeyService — argon2id + sha256+pepper двойной hash для O(1) lookup.

Pepper защищает от offline rainbow-table при DB dump (D-08).
Plaintext key возвращается ровно один раз через mint response; в DB
никогда не хранится. last_used_at дебансится in-process на single-worker
(Pitfall F + PROJECT.md Assumption A6).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.auth.user import User
from keenyspace_server.db.models import ApiKey

log = structlog.get_logger(__name__)
_PH = PasswordHasher()


def _generate_key_body() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _full_key(body: str) -> str:
    return f"ks_live_{body}"


def _compute_lookup_hash(body: str, pepper: str) -> str:
    return hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()


DbFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class ApiKeyService:
    def __init__(
        self,
        *,
        pepper: str,
        db_factory: DbFactory,
        debounce_seconds: int = 300,
    ) -> None:
        self._pepper = pepper
        self._db_factory = db_factory
        self._debounce = debounce_seconds
        self._last_used_writes: dict[UUID, datetime] = {}

    async def mint(self, *, user_sub: str, name: str) -> Mapping[str, Any]:
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
        )
        async with self._db_factory() as session:
            session.add(row)
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
        }

    async def verify(self, plaintext: str) -> User | None:
        if not plaintext.startswith("ks_live_"):
            return None
        body = plaintext[len("ks_live_") :]
        lookup_hash = _compute_lookup_hash(body, self._pepper)
        async with self._db_factory() as session:
            result = await session.execute(
                select(ApiKey).where(
                    ApiKey.lookup_hash == lookup_hash,
                    ApiKey.revoked_at.is_(None),
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        try:
            await asyncio.to_thread(_PH.verify, row.hash, body)
        except VerifyMismatchError, VerificationError, InvalidHashError:
            return None
        await self._maybe_touch_last_used(row.id)
        return User(sub=row.user_sub, _display_name=row.user_sub, source="api_key")

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
            await session.commit()
        if cursor.rowcount > 0:
            log.info(
                "auth.api_key.revoked",
                key_id=str(key_id),
                user_sub=user_sub,
            )
            return True
        return False

    async def _maybe_touch_last_used(self, key_id: UUID) -> None:
        now = datetime.now(UTC)
        last = self._last_used_writes.get(key_id)
        if last is not None and (now - last).total_seconds() < self._debounce:
            return
        self._last_used_writes[key_id] = now
        async with self._db_factory() as session:
            await session.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=now)
            )
            await session.commit()
