"""ApiKeyService module surface: key-generation helpers and constructor.

Pins the key shape produced by the pure helpers — a 43-char body, the ks_live_ prefix,
and a SHA-256 lookup hash over body+pepper. Behavioural mint/verify/list/revoke coverage
lives in test_api_keys_router.py and test_api_key_authz.py.
"""

from __future__ import annotations

import hashlib

PEPPER = "test-pepper-32chars-padded-here!"


def test_pure_helpers_exist() -> None:
    from keenyspace_server.auth.api_keys import (
        _compute_lookup_hash,
        _full_key,
        _generate_key_body,
    )

    body = _generate_key_body()
    assert len(body) == 43
    assert _full_key(body).startswith("ks_live_")
    h = _compute_lookup_hash(body, PEPPER)
    assert h == hashlib.sha256(f"{body}{PEPPER}".encode()).hexdigest()
    assert len(h) == 64


def test_api_key_service_constructible() -> None:
    from contextlib import asynccontextmanager

    from keenyspace_server.auth.api_keys import ApiKeyService

    @asynccontextmanager
    async def _noop_factory():  # type: ignore[no-untyped-def]
        yield None

    svc = ApiKeyService(pepper=PEPPER, db_factory=_noop_factory, debounce_seconds=300)
    assert svc is not None
