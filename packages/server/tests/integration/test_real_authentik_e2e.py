"""End-to-end regression against a real Authentik.

Uses testcontainers DockerCompose to spin up a real Authentik 2024.10 stack,
applies the keenyspace blueprint, obtains a signed JWT via client_credentials,
and validates it through OidcClient.validate_access_token.

The regression it guards: Authentik per_provider issuer_mode mints tokens whose `iss`
carries a trailing slash (http://host/application/o/<slug>/), which a naive equality
check against the configured issuer rejects. This proves a real Authentik-signed
trailing-slash iss validates end-to-end through the custom iss validator.

Acceptable gaps (by design):
  - Does NOT exercise interactive device-code approval UI (operator SMOKE.md owns that).
  - Does NOT exercise keenyspace login --device-code CLI subprocess path.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.real_idp

# test-only public dev placeholder (mirrors AUTHENTIK_BOOTSTRAP_TOKEN in docker-compose.yml)
_TEST_BOOTSTRAP_TOKEN = "authentik-bootstrap-token-replace-me-32-bytes-padding"


def wait_for_blueprint(base_url: str, token: str, timeout: int = 90) -> None:
    """Poll until the keenyspace application is provisioned by the blueprint.

    Primary check: admin API application lookup via bootstrap token.
    Fallback check: attempt client_credentials token acquisition directly,
    which verifies the application + provider are functional (handles the case
    where the bootstrap token has expired in a long-running dev stack but the
    blueprint has already been applied).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                f"{base_url}/api/v3/core/applications/",
                params={"slug": "keenyspace"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if r.status_code == 200 and r.json().get("results"):
                return
        except Exception:
            pass

        # Fallback: the bootstrap token may have expired in a long-running stack
        # (e.g. dev environment). Directly probe the token endpoint — if we can
        # obtain a token for the test service account, the blueprint was applied.
        try:
            probe = httpx.post(
                f"{base_url}/application/o/token/",
                data={
                    "grant_type": "client_credentials",
                    "client_id": "keenyspace-cli",
                    "username": "ks-test-svc",
                    "password": "ks-test-app-password-known-value-32chars",
                    "scope": "openid",
                },
                timeout=5,
            )
            if probe.status_code == 200 and "access_token" in probe.json():
                return
        except Exception:
            pass

        time.sleep(2)
    raise TimeoutError("Blueprint application timed out after waiting for keenyspace application")


def get_ks_access_token(base_url: str) -> str:
    resp = httpx.post(
        f"{base_url}/application/o/token/",
        data={
            "grant_type": "client_credentials",
            "client_id": "keenyspace-cli",
            "username": "ks-test-svc",
            "password": "ks-test-app-password-known-value-32chars",
            "scope": "openid profile email",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


@pytest.fixture(scope="session")
def authentik_stack(request: pytest.FixtureRequest) -> Generator[str]:
    from testcontainers.compose import DockerCompose
    from testcontainers.core.wait_strategies import HttpWaitStrategy

    # The overlay pins host port 9000 (token iss depends on it); a running dev
    # stack holding that port would make the lane fail or bind the wrong IdP.
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 9000)) == 0:
            pytest.skip("127.0.0.1:9000 is in use (stop the dev stack to run this lane)")

    # testcontainers DockerCompose has no project-name parameter; without an
    # explicit project, compose defaults to the directory name ("deploy") and
    # would reconfigure and tear down the developer's own stack.
    previous_project = os.environ.get("COMPOSE_PROJECT_NAME")
    os.environ["COMPOSE_PROJECT_NAME"] = "ks-real-idp"

    def restore_project_name() -> None:
        if previous_project is None:
            os.environ.pop("COMPOSE_PROJECT_NAME", None)
        else:
            os.environ["COMPOSE_PROJECT_NAME"] = previous_project

    request.addfinalizer(restore_project_name)

    deploy_dir = str(Path(__file__).parents[4] / "deploy")
    compose = DockerCompose(
        context=deploy_dir,
        compose_file_name=["docker-compose.yml", "docker-compose.authentik-test.yml"],
        env_file="authentik-test.env",
    )
    compose.waiting_for(
        {
            "authentik": HttpWaitStrategy(9000, "/-/health/ready/")
            .for_status_code(200)
            .with_startup_timeout(180),
        }
    )
    with compose:
        # Static URL: docker-compose.authentik-test.yml pins host port 9000:9000.
        # Dynamic port discovery is forbidden here — a random mapped port would
        # change the per_provider token iss (Authentik builds it from request host)
        # and break the trailing-slash iss assertion below.
        base_url = "http://localhost:9000"
        wait_for_blueprint(base_url, _TEST_BOOTSTRAP_TOKEN)
        yield base_url


@pytest.mark.asyncio
async def test_trailing_slash_iss_validates_end_to_end(authentik_stack: str) -> None:
    """A real Authentik trailing-slash iss validates through the custom iss validator.

    Asserts:
    1. Authentik per_provider really does mint an iss with a trailing slash.
    2. OidcClient.validate_access_token accepts that real JWT and returns a User with
       source="oidc" — no mock involved: a real JWKS fetch and a real iss check.
    """
    base_url = authentik_stack
    access_token = get_ks_access_token(base_url)

    # Decode WITHOUT signature verification to inspect raw claims.
    # validate_access_token does the real signature check below.
    header_b64 = access_token.split(".")[0]
    import base64

    padded = header_b64 + "=" * (-len(header_b64) % 4)
    import json

    _header = json.loads(base64.urlsafe_b64decode(padded))
    payload_b64 = access_token.split(".")[1]
    padded2 = payload_b64 + "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded2))

    iss_claim = claims.get("iss", "")
    assert iss_claim.endswith("/"), (
        f"Authentik per_provider iss must have trailing slash; got: {iss_claim!r}"
    )
    assert iss_claim == f"{base_url}/application/o/keenyspace/", (
        f"Expected iss=http://localhost:9000/application/o/keenyspace/, got {iss_claim!r}"
    )

    from keenyspace_server.auth.oidc import OidcClient, build_oauth
    from keenyspace_server.config import get_settings

    get_settings.cache_clear()

    saved_env: dict[str, str | None] = {}
    env_vars = {
        "KEENYSPACE_DB__URL": "postgresql+asyncpg://x:x@localhost/x",
        "KEENYSPACE_FS__ROOT": "/tmp/ks-e2e-test",
        "KEENYSPACE_AUTH__OIDC_ISSUER_URL": f"{base_url}/application/o/keenyspace/",
        "KEENYSPACE_AUTH__OIDC_CLIENT_ID": "keenyspace-cli",
        "KEENYSPACE_AUTH__OIDC_CLIENT_SECRET": "not-used-public-client-no-secret",
        "KEENYSPACE_AUTH__OIDC_REDIRECT_URI": "http://localhost:8000/v1/api/auth/callback",
        "KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI": "http://localhost:8000/",
        "KEENYSPACE_AUTH__SESSION_SECRET_KEY": "test-session-secret-32chars-pad!",
        "KEENYSPACE_AUTH__API_KEY_PEPPER": "test-pepper-32chars-padded-here!",
    }
    try:
        for k, v in env_vars.items():
            saved_env[k] = os.environ.get(k)
            os.environ[k] = v

        get_settings.cache_clear()
        settings = get_settings()
        oauth = build_oauth(settings)
        oidc_client = OidcClient(oauth, settings.auth)

        user = await oidc_client.validate_access_token(access_token)
    finally:
        for k, orig in saved_env.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig
        get_settings.cache_clear()

    assert user is not None, (
        "OidcClient.validate_access_token returned None for real Authentik JWT; "
        "check auth.token.iss_mismatch in logs — the iss validator may not be wired"
    )
    assert user.source == "oidc", f"Expected source=oidc, got {user.source!r}"
    assert isinstance(user.sub, str) and len(user.sub) > 0, (
        f"Expected non-empty sub, got {user.sub!r}"
    )
