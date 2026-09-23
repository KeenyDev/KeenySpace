"""JwksCache — 1h TTL, serve-stale-on-error, throttled force-refresh on unknown kid.

Serves the signing keys used to validate access-token JWTs (the ks_at cookie
and OIDC Bearer tokens). id_token validation in the OIDC callback is a
separate code path that uses Authlib's own cache.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
import structlog
from joserfc.jwk import KeySet

log = structlog.get_logger(__name__)


class JwksCache:
    def __init__(
        self,
        jwks_uri_provider: Callable[[], Awaitable[str]],
        *,
        ttl_seconds: int = 3600,
        min_retry_interval_seconds: int = 30,
    ) -> None:
        self._jwks_uri_provider = jwks_uri_provider
        self._ttl = ttl_seconds
        self._min_retry = min_retry_interval_seconds
        self._keyset: KeySet | None = None
        self._fetched_at: float = 0.0
        self._last_failed_at: float = 0.0
        self._last_attempt_at: float | None = None
        self._lock = asyncio.Lock()

    def _is_fresh(self, now: float) -> bool:
        return self._keyset is not None and (now - self._fetched_at) < self._ttl

    async def get(self) -> KeySet | None:
        if self._is_fresh(time.monotonic()):
            return self._keyset
        async with self._lock:
            now = time.monotonic()
            if self._is_fresh(now):
                return self._keyset
            if (now - self._last_failed_at) < self._min_retry:
                if self._keyset is not None:
                    log.warning("auth.jwks.serving_stale")
                    return self._keyset
                return None
            await self._fetch(now)
            return self._keyset

    async def force_refresh(self) -> KeySet | None:
        # Unknown-kid refreshes are attacker-triggerable (any unauthenticated
        # request can carry a random kid), so they share the min-retry throttle.
        async with self._lock:
            now = time.monotonic()
            if self._last_attempt_at is not None and (now - self._last_attempt_at) < self._min_retry:
                log.debug("auth.jwks.force_refresh_throttled")
                return self._keyset
            await self._fetch(now)
            return self._keyset

    async def _fetch(self, now: float) -> None:
        self._last_attempt_at = now
        try:
            uri = await self._jwks_uri_provider()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(uri)
                resp.raise_for_status()
            self._keyset = KeySet.import_key_set(resp.json())
            self._fetched_at = now
            log.info("auth.jwks.refreshed")
        except Exception:
            self._last_failed_at = now
            log.warning("auth.jwks.fetch_failed")
