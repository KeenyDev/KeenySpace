from __future__ import annotations

import importlib
from pathlib import Path

from pytest_httpserver import HTTPServer
from typer.testing import CliRunner


def _reload_and_get_app(server_url: str) -> object:
    import os

    os.environ["KEENYSPACE_SERVER_URL"] = server_url
    os.environ["COLUMNS"] = "200"
    import keenyspace.paths as paths_mod
    importlib.reload(paths_mod)
    import keenyspace.config as cfg
    importlib.reload(cfg)
    cfg.get_client_settings.cache_clear()  # type: ignore[attr-defined]
    import keenyspace.auth as auth_mod
    importlib.reload(auth_mod)
    import keenyspace.clients.http as http_mod
    importlib.reload(http_mod)
    import keenyspace.cli.status as status_mod
    importlib.reload(status_mod)
    import keenyspace.__main__ as main_mod
    return importlib.reload(main_mod)


def test_status_unreachable_server(
    temp_config_dir: dict[str, Path], cli_runner: CliRunner
) -> None:
    # Point at a closed port — no server bound.
    main_mod = _reload_and_get_app("http://127.0.0.1:1")
    result = cli_runner.invoke(main_mod.app, ["status"])  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    assert "unreachable" in result.output


def test_status_authenticated_user(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/v1/api/auth/api-keys").respond_with_json([])
    # Seed auth.json under CONFIG_DIR (mode 0600) so identity probe finds it.
    import json
    import os
    import stat

    auth_path = temp_config_dir["config_dir"] / "auth.json"
    auth_path.write_text(json.dumps({"api_key": "ks_live_testuserkey_abc"}))
    os.chmod(auth_path, stat.S_IRUSR | stat.S_IWUSR)

    # Force IPv4 — pytest_httpserver binds 127.0.0.1 but localhost resolves
    # to ::1 first on this host, which httpx cannot reach.
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(main_mod.app, ["status"])  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    assert "ks_live_test" in result.output


def test_status_missing_auth_file(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/v1/api/auth/api-keys").respond_with_json(
        {"detail": "Unauthorized"}, status=401
    )
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(main_mod.app, ["status"])  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    assert "(missing)" in result.output
    assert "not authenticated" in result.output
