from __future__ import annotations

import importlib
import json
import stat
from pathlib import Path

import pytest
import yaml
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
    import keenyspace.__main__ as main_mod
    importlib.reload(main_mod)
    import keenyspace.cli.workspace as ws_mod
    importlib.reload(ws_mod)
    import keenyspace.workspace_inference as inf
    importlib.reload(inf)
    return main_mod


def _ipv4(url: str) -> str:
    return url.replace("localhost", "127.0.0.1")


def _seed_auth(config_dir: Path) -> None:
    import os as _os

    auth = config_dir / "auth.json"
    auth.write_text(json.dumps({"api_key": "ks_live_testkey"}))
    _os.chmod(auth, stat.S_IRUSR | stat.S_IWUSR)


def test_list_calls_server_renders_table(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/", method="GET"
    ).respond_with_json(
        {
            "workspaces": [
                {
                    "slug": "demo",
                    "blueprint_pin": "default@v0.1",
                    "status": "active",
                    "last_compile_at": None,
                }
            ]
        }
    )
    server_url = _ipv4(httpserver.url_for(""))
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(main_mod.app, ["workspace", "list"])  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    assert "demo" in result.output
    assert "default@v0.1" in result.output


def test_use_writes_config_yaml(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/demo", method="GET"
    ).respond_with_json({"slug": "demo", "status": "active"})
    server_url = _ipv4(httpserver.url_for(""))
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(main_mod.app, ["workspace", "use", "demo"])  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    cfg_path = temp_config_dir["config_dir"] / "config.yaml"
    assert cfg_path.exists()
    parsed = yaml.safe_load(cfg_path.read_text())
    assert parsed["default_workspace"] == "demo"


def test_use_workspace_not_found_404(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/missing", method="GET"
    ).respond_with_json({"detail": "not found"}, status=404)
    server_url = _ipv4(httpserver.url_for(""))
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(main_mod.app, ["workspace", "use", "missing"])  # type: ignore[attr-defined]
    assert result.exit_code != 0
    cfg_path = temp_config_dir["config_dir"] / "config.yaml"
    assert not cfg_path.exists()


def test_archive_calls_server(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/demo/archive", method="POST"
    ).respond_with_json(
        {"slug": "demo", "status": "archived", "archived_at": "2026-05-24T00:00:00Z"}
    )
    server_url = _ipv4(httpserver.url_for(""))
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(main_mod.app, ["workspace", "archive", "demo"])  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    assert "archived" in result.output


def test_from_cwd_env_var_wins(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEENYSPACE_WORKSPACE", "alpha")
    main_mod = _reload_and_get_app("http://127.0.0.1:1")
    result = cli_runner.invoke(main_mod.app, ["workspace", "from-cwd"])  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "env" in result.output


def test_from_cwd_unresolved_exits_2(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("KEENYSPACE_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    main_mod = _reload_and_get_app("http://127.0.0.1:1")
    result = cli_runner.invoke(main_mod.app, ["workspace", "from-cwd"])  # type: ignore[attr-defined]
    assert result.exit_code == 2
    assert "unresolved" in result.output


def test_workspace_subcommands_reachable_from_cli_entrypoint() -> None:
    """Regression guard (Phase 6 dogfood P1): `workspace` subcommands must be
    dispatchable through the real CLI entrypoint, not just importable.

    The WR-04 lazy import previously lived in @workspace_app.callback(), which
    Click runs AFTER subcommand resolution, so `keenyspace workspace use` died
    with "No such command 'use'". The fix imports keenyspace.cli.workspace
    eagerly when sys.argv[1] == "workspace".

    We invoke the entry point exactly as the console script does
    (`from keenyspace.__main__ import app; app()`) in a fresh process with a
    workspace argv. NOTE: `python -m keenyspace` is NOT used — under -m the
    module runs as __main__, so workspace.py's `from keenyspace.__main__ import
    workspace_app` would bind a duplicate module and registration would be
    invisible (a -m harness artifact, not a product bug).
    """
    import os
    import subprocess
    import sys

    code = (
        "import sys; sys.argv = ['keenyspace', 'workspace', '--help']; "
        "from keenyspace.__main__ import app; app()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=10,
        env={**os.environ, "COLUMNS": "200"},
    )
    out = result.stdout.decode()
    assert "use" in out and "list" in out and "archive" in out, out
    assert "No such command" not in result.stderr.decode()
