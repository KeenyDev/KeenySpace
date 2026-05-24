from __future__ import annotations

import importlib
import json
import os
import stat
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer


def _reload() -> object:
    import keenyspace.paths as paths_mod
    importlib.reload(paths_mod)
    import keenyspace.config as cfg
    importlib.reload(cfg)
    cfg.get_client_settings.cache_clear()  # type: ignore[attr-defined]
    import keenyspace.auth as auth_mod
    importlib.reload(auth_mod)
    import keenyspace.cli.login as login_mod
    return importlib.reload(login_mod)


def _ipv4(url: str) -> str:
    return url.replace("localhost", "127.0.0.1")


def _setup_server(
    httpserver: HTTPServer,
    *,
    server_url: str | None = None,
) -> tuple[str, str]:
    """Mock both the server discovery + Authentik device/token endpoints on
    the same httpserver. Returns (server_url, authentik_issuer)."""
    url = server_url or _ipv4(httpserver.url_for(""))
    issuer = url.rstrip("/")
    httpserver.expect_request("/v1/api/auth/discovery").respond_with_json(
        {"issuer": issuer}
    )
    return url, issuer


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture(autouse=True)
def _patch_asyncio_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the device-code interval wait in tests."""
    monkeypatch.setattr("keenyspace.cli.login.asyncio.sleep", _no_sleep)


async def test_login_happy_path(
    temp_config_dir: dict[str, Path],
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_url, issuer = _setup_server(httpserver)
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)
    login_mod = _reload()

    httpserver.expect_request(
        "/application/o/device/", method="POST"
    ).respond_with_json(
        {
            "device_code": "dev",
            "user_code": "ABCD-1234",
            "verification_uri": f"{issuer}/device",
            "verification_uri_complete": f"{issuer}/device?user_code=ABCD-1234",
            "expires_in": 600,
            "interval": 1,
        }
    )

    call_state = {"calls": 0}

    def _token_handler(_request: object) -> tuple[str, int, dict[str, str]]:
        from werkzeug.wrappers import Response

        call_state["calls"] += 1
        if call_state["calls"] == 1:
            return Response(json.dumps({"error": "authorization_pending"}), status=400)  # type: ignore[return-value]
        return Response(  # type: ignore[return-value]
            json.dumps(
                {
                    "access_token": "tok123",
                    "refresh_token": "ref456",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            ),
            status=200,
        )

    httpserver.expect_request(
        "/application/o/token/", method="POST"
    ).respond_with_handler(_token_handler)

    await login_mod.run_login(server_url=None)  # type: ignore[attr-defined]

    auth_path = temp_config_dir["config_dir"] / "auth.json"
    assert auth_path.exists()
    payload = json.loads(auth_path.read_text())
    assert payload["access_token"] == "tok123"
    assert payload["refresh_token"] == "ref456"
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600


async def test_login_slow_down_increases_interval(
    temp_config_dir: dict[str, Path],
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_url, issuer = _setup_server(httpserver)
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)
    login_mod = _reload()

    intervals: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        intervals.append(seconds)

    monkeypatch.setattr("keenyspace.cli.login.asyncio.sleep", _record_sleep)

    httpserver.expect_request(
        "/application/o/device/", method="POST"
    ).respond_with_json(
        {
            "device_code": "dev",
            "user_code": "U-X",
            "verification_uri_complete": f"{issuer}/d?u=X",
            "expires_in": 600,
            "interval": 1,
        }
    )

    call_state = {"calls": 0}

    def _token_handler(_request: object) -> tuple[str, int, dict[str, str]]:
        from werkzeug.wrappers import Response

        call_state["calls"] += 1
        if call_state["calls"] == 1:
            return Response(json.dumps({"error": "slow_down"}), status=400)  # type: ignore[return-value]
        return Response(  # type: ignore[return-value]
            json.dumps({"access_token": "tok", "expires_in": 60}), status=200
        )

    httpserver.expect_request(
        "/application/o/token/", method="POST"
    ).respond_with_handler(_token_handler)

    await login_mod.run_login(server_url=None)  # type: ignore[attr-defined]

    assert len(intervals) >= 2
    assert intervals[1] == intervals[0] + 5


async def test_login_access_denied_raises(
    temp_config_dir: dict[str, Path],
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_url, issuer = _setup_server(httpserver)
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)
    login_mod = _reload()

    httpserver.expect_request(
        "/application/o/device/", method="POST"
    ).respond_with_json(
        {
            "device_code": "dev",
            "user_code": "U",
            "verification_uri_complete": f"{issuer}/d?u=U",
            "expires_in": 600,
            "interval": 1,
        }
    )
    httpserver.expect_request(
        "/application/o/token/", method="POST"
    ).respond_with_json({"error": "access_denied"}, status=400)

    with pytest.raises(RuntimeError, match="access_denied"):
        await login_mod.run_login(server_url=None)  # type: ignore[attr-defined]


async def test_login_expired_token_raises(
    temp_config_dir: dict[str, Path],
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_url, issuer = _setup_server(httpserver)
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)
    login_mod = _reload()

    httpserver.expect_request(
        "/application/o/device/", method="POST"
    ).respond_with_json(
        {
            "device_code": "dev",
            "user_code": "U",
            "verification_uri_complete": f"{issuer}/d?u=U",
            "expires_in": 600,
            "interval": 1,
        }
    )
    httpserver.expect_request(
        "/application/o/token/", method="POST"
    ).respond_with_json({"error": "expired_token"}, status=400)

    with pytest.raises(RuntimeError, match="expired_token"):
        await login_mod.run_login(server_url=None)  # type: ignore[attr-defined]


async def test_login_deadline_exceeded_raises_timeout(
    temp_config_dir: dict[str, Path],
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_url, issuer = _setup_server(httpserver)
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)
    login_mod = _reload()

    # Advance loop.time so the deadline expires after the first poll.
    step_state = {"ticks": 0}

    class _FakeLoop:
        def time(self) -> float:
            step_state["ticks"] += 1
            return 100.0 if step_state["ticks"] > 2 else 0.0

    monkeypatch.setattr(
        "keenyspace.cli.login.asyncio.get_event_loop", lambda: _FakeLoop()
    )

    httpserver.expect_request(
        "/application/o/device/", method="POST"
    ).respond_with_json(
        {
            "device_code": "dev",
            "user_code": "U",
            "verification_uri_complete": f"{issuer}/d?u=U",
            "expires_in": 1,
            "interval": 1,
        }
    )
    httpserver.expect_request(
        "/application/o/token/", method="POST"
    ).respond_with_json({"error": "authorization_pending"}, status=400)

    with pytest.raises(TimeoutError, match="expired"):
        await login_mod.run_login(server_url=None)  # type: ignore[attr-defined]


async def test_logout_clears_auth_json_and_calls_server(
    temp_config_dir: dict[str, Path],
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)
    login_mod = _reload()

    auth_path = temp_config_dir["config_dir"] / "auth.json"
    auth_path.write_text(json.dumps({"access_token": "seed-tok"}))
    os.chmod(auth_path, 0o600)

    captured: dict[str, str] = {}

    def _logout_handler(request: object) -> tuple[str, int, dict[str, str]]:
        from werkzeug.wrappers import Response

        captured["auth"] = request.headers.get("Authorization", "")  # type: ignore[attr-defined]
        return Response(json.dumps({"ok": True}), status=200)  # type: ignore[return-value]

    httpserver.expect_request(
        "/v1/api/auth/logout", method="POST"
    ).respond_with_handler(_logout_handler)

    await login_mod.run_logout()  # type: ignore[attr-defined]

    assert not auth_path.exists()
    assert captured.get("auth") == "Bearer seed-tok"


async def test_logout_when_already_logged_out(
    temp_config_dir: dict[str, Path],
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)
    login_mod = _reload()

    auth_path = temp_config_dir["config_dir"] / "auth.json"
    assert not auth_path.exists()

    # No exception expected (idempotent).
    await login_mod.run_logout()  # type: ignore[attr-defined]
