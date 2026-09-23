"""JwksCache: TTL expiry, serving stale keys on failure, throttled force-refresh."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from keenyspace_server.auth.jwks_cache import JwksCache

SAMPLE_JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "kid": "k1",
            "use": "sig",
            "alg": "RS256",
            "n": (
                "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhD"
                "R1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6C"
                "f0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1"
                "n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1"
                "jF44-csFCur-kEgU8awapJzKnqDKgw"
            ),
            "e": "AQAB",
        }
    ]
}


def _patch_client(response_or_exc):
    mock_cli = patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient")
    return mock_cli


@pytest.mark.asyncio
async def test_get_fetches_on_cache_miss() -> None:
    provider = AsyncMock(return_value="https://idp/jwks")
    cache = JwksCache(provider, ttl_seconds=3600, min_retry_interval_seconds=30)
    fake_resp = MagicMock()
    fake_resp.json.return_value = SAMPLE_JWKS
    fake_resp.raise_for_status = MagicMock()
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)
        keyset = await cache.get()
    assert keyset is not None
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_ttl_expiry_triggers_refetch() -> None:
    """Inside the 1h TTL the cached keyset is reused; past it, the JWKS is refetched."""
    provider = AsyncMock(return_value="https://idp/jwks")
    cache = JwksCache(provider, ttl_seconds=3600, min_retry_interval_seconds=30)
    fake_resp = MagicMock()
    fake_resp.json.return_value = SAMPLE_JWKS
    fake_resp.raise_for_status = MagicMock()
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)
        await cache.get()
        cache._fetched_at -= 100
        await cache.get()
        assert mock_cli.call_count == 1
        cache._fetched_at -= 4000
        await cache.get()
        assert mock_cli.call_count == 2


@pytest.mark.asyncio
async def test_serves_stale_on_fetch_failure() -> None:
    provider = AsyncMock(return_value="https://idp/jwks")
    cache = JwksCache(provider, ttl_seconds=3600, min_retry_interval_seconds=30)
    fake_resp = MagicMock()
    fake_resp.json.return_value = SAMPLE_JWKS
    fake_resp.raise_for_status = MagicMock()
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)
        keyset_first = await cache.get()
        assert keyset_first is not None
    cache._fetched_at -= 4000
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=Exception("connection refused")
        )
        keyset_stale = await cache.get()
    assert keyset_stale is keyset_first
    keyset_second = await cache.get()
    assert keyset_second is keyset_first


@pytest.mark.asyncio
async def test_force_refresh_on_unknown_kid() -> None:
    provider = AsyncMock(return_value="https://idp/jwks")
    cache = JwksCache(provider, ttl_seconds=3600, min_retry_interval_seconds=30)
    fake_resp = MagicMock()
    fake_resp.json.return_value = SAMPLE_JWKS
    fake_resp.raise_for_status = MagicMock()
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)
        await cache.get()
        cache._last_attempt_at -= 31
        await cache.force_refresh()
        assert mock_cli.call_count == 2


@pytest.mark.asyncio
async def test_force_refresh_throttled_within_min_retry_interval() -> None:
    provider = AsyncMock(return_value="https://idp/jwks")
    cache = JwksCache(provider, ttl_seconds=3600, min_retry_interval_seconds=30)
    fake_resp = MagicMock()
    fake_resp.json.return_value = SAMPLE_JWKS
    fake_resp.raise_for_status = MagicMock()
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)
        first = await cache.get()
        results = [await cache.force_refresh() for _ in range(20)]
        assert mock_cli.call_count == 1
    assert all(r is first for r in results)


@pytest.mark.asyncio
async def test_force_refresh_throttled_after_failure() -> None:
    provider = AsyncMock(return_value="https://idp/jwks")
    cache = JwksCache(provider, ttl_seconds=3600, min_retry_interval_seconds=30)
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=Exception("idp down")
        )
        assert await cache.force_refresh() is None
        assert await cache.force_refresh() is None
        assert mock_cli.call_count == 1


@pytest.mark.asyncio
async def test_empty_cache_and_idp_down_returns_none() -> None:
    """Cold boot with the IdP unreachable returns None, so the OIDC bearer path 401s."""
    provider = AsyncMock(return_value="https://idp/jwks")
    cache = JwksCache(provider, ttl_seconds=3600, min_retry_interval_seconds=30)
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=Exception("idp down")
        )
        result = await cache.get()
    assert result is None
