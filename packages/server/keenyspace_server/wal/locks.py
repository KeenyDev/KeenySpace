from __future__ import annotations

import asyncio
from uuid import UUID

from ulid import ULID


class WorkspaceLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_ids: dict[str, ULID] = {}
        self._registry_lock: asyncio.Lock | None = None

    async def for_workspace(self, ws_uuid: UUID) -> asyncio.Lock:
        if self._registry_lock is None:
            self._registry_lock = asyncio.Lock()
        key = str(ws_uuid)
        async with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
        return lock

    def last_id(self, ws_uuid: UUID) -> ULID | None:
        """Last WAL entry id issued for the workspace; read only under its lock."""
        return self._last_ids.get(str(ws_uuid))

    def record_id(self, ws_uuid: UUID, entry_id: ULID) -> None:
        """Remember the latest issued WAL entry id; call only under the workspace lock."""
        self._last_ids[str(ws_uuid)] = entry_id
