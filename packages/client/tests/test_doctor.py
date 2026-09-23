"""`keenyspace doctor` runs at least 7 checks, with JSON output and warn/fail flags."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

from pytest_httpserver import HTTPServer
from typer.testing import CliRunner


def _reload_and_get_app(server_url: str) -> object:
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
    import keenyspace.cli.doctor as doctor_mod

    importlib.reload(doctor_mod)
    import keenyspace.__main__ as main_mod

    return importlib.reload(main_mod)


def _seed_auth(config_dir: Path, *, mode: int = 0o600) -> None:
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"api_key": "ks_live_test"}))
    os.chmod(auth_path, mode)


def test_doctor_runs_at_least_7_checks_json(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/readyz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/v1/api/auth/api-keys").respond_with_json([])
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["doctor", "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) >= 7
    for row in data:
        assert "name" in row and "status" in row and "detail" in row
        assert row["status"] in ("ok", "warn", "fail")


def test_doctor_json_output_machine_readable(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/readyz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/v1/api/auth/api-keys").respond_with_json([])
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["doctor", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    names = {row["name"] for row in payload}
    assert "server /healthz" in names
    assert "daemon socket" in names


def test_doctor_reports_no_daemon_socket(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/readyz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/v1/api/auth/api-keys").respond_with_json([])
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["doctor", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    row = next(r for r in payload if r["name"] == "daemon socket")
    assert row["status"] == "warn"


def test_doctor_reports_dropped_events_warn(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    state_dir = temp_config_dir["state_dir"]
    (state_dir / "dropped.json").write_text(json.dumps({"by_kind": {"post-tool": {"count": 5}}}))
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/readyz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/v1/api/auth/api-keys").respond_with_json([])
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["doctor", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    row = next(r for r in payload if r["name"] == "dropped events")
    assert row["status"] == "warn"
    assert "5" in row["detail"]


def test_doctor_server_unreachable(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    main_mod = _reload_and_get_app("http://127.0.0.1:1")
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["doctor", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    row = next(r for r in payload if r["name"] == "server /healthz")
    assert row["status"] == "fail"


def test_doctor_table_default_output(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    """Without --json the output should be a rich table (contains 'Check')."""
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/readyz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/v1/api/auth/api-keys").respond_with_json([])
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["doctor"],
    )
    assert result.exit_code == 0, result.output
    assert "keenyspace doctor" in result.output
    assert "Check" in result.output
