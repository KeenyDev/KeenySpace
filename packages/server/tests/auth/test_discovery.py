"""The public /v1/api/auth/discovery IdP-issuer shim.

`keenyspace login` calls this first to learn the Authentik issuer; it must be
reachable anonymously (it is the bootstrap of the auth flow) and return the
configured OIDC issuer with no trailing slash.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_discovery_is_public_and_returns_issuer(anon_client) -> None:
    resp = await anon_client.get("/v1/api/auth/discovery")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["issuer"] == "http://localhost:9999/application/o/test"


@pytest.mark.asyncio
async def test_discovery_issuer_has_no_trailing_slash(anon_client) -> None:
    resp = await anon_client.get("/v1/api/auth/discovery")
    assert resp.status_code == 200, resp.text
    assert not body_issuer(resp).endswith("/")


def body_issuer(resp) -> str:  # type: ignore[no-untyped-def]
    issuer = resp.json()["issuer"]
    assert isinstance(issuer, str)
    return issuer
