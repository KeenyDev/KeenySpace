from __future__ import annotations

import asyncio
from uuid import UUID


class WorkspaceLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
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
