"""Tests for workspace register / unregister / registrations subcommands."""

from __future__ import annotations

import importlib
import json
import stat
from pathlib import Path
from unittest.mock import patch

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


def test_register_writes_map_entry(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/myws", method="GET"
    ).respond_with_json({"slug": "myws", "status": "active"})
    server_url = _ipv4(httpserver.url_for(""))
    main_mod = _reload_and_get_app(server_url)
    target = str(temp_config_dir["home"] / "projects" / "repo")
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "register", "myws", target],
    )
    assert result.exit_code == 0, result.output
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    assert map_path.exists()
    parsed = yaml.safe_load(map_path.read_text())
    assert parsed["paths"][target] == "myws"


def test_register_preserves_existing_keys(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/ws2", method="GET"
    ).respond_with_json({"slug": "ws2", "status": "active"})
    server_url = _ipv4(httpserver.url_for(""))
    existing_path = str(temp_config_dir["home"] / "old")
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    map_path.write_text(yaml.safe_dump({"paths": {existing_path: "old-ws"}}))
    main_mod = _reload_and_get_app(server_url)
    new_path = str(temp_config_dir["home"] / "new")
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "register", "ws2", new_path],
    )
    assert result.exit_code == 0, result.output
    parsed = yaml.safe_load(map_path.read_text())
    assert parsed["paths"][existing_path] == "old-ws"
    assert parsed["paths"][new_path] == "ws2"


def test_register_idempotent_same_slug(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/myws", method="GET"
    ).respond_with_json({"slug": "myws", "status": "active"})
    server_url = _ipv4(httpserver.url_for(""))
    target = str(temp_config_dir["home"] / "repo")
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    map_path.write_text(yaml.safe_dump({"paths": {target: "myws"}}))
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "register", "myws", target],
    )
    assert result.exit_code == 0, result.output
    parsed = yaml.safe_load(map_path.read_text())
    assert list(parsed["paths"].keys()).count(target) == 1


def test_register_refuses_different_slug_without_force(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/new-ws", method="GET"
    ).respond_with_json({"slug": "new-ws", "status": "active"})
    server_url = _ipv4(httpserver.url_for(""))
    target = str(temp_config_dir["home"] / "repo")
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    map_path.write_text(yaml.safe_dump({"paths": {target: "old-ws"}}))
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "register", "new-ws", target],
    )
    assert result.exit_code == 1
    parsed = yaml.safe_load(map_path.read_text())
    assert parsed["paths"][target] == "old-ws"


def test_register_force_overwrites(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/new-ws", method="GET"
    ).respond_with_json({"slug": "new-ws", "status": "active"})
    server_url = _ipv4(httpserver.url_for(""))
    target = str(temp_config_dir["home"] / "repo")
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    map_path.write_text(yaml.safe_dump({"paths": {target: "old-ws"}}))
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "register", "new-ws", target, "--force"],
    )
    assert result.exit_code == 0, result.output
    parsed = yaml.safe_load(map_path.read_text())
    assert parsed["paths"][target] == "new-ws"


def test_register_marker_writes_json_not_map(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/myws", method="GET"
    ).respond_with_json({"slug": "myws", "status": "active"})
    server_url = _ipv4(httpserver.url_for(""))
    target = temp_config_dir["home"] / "repo"
    target.mkdir(parents=True, exist_ok=True)
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "register", "myws", str(target), "--marker"],
    )
    assert result.exit_code == 0, result.output
    marker = target / ".keenyspace" / "slug-marker.json"
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["slug"] == "myws"
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    assert not map_path.exists()


def test_register_offline_still_writes(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    server_url = "http://127.0.0.1:19999"
    target = str(temp_config_dir["home"] / "repo")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "register", "myws", target],
    )
    assert result.exit_code == 0, result.output
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    assert map_path.exists()
    parsed = yaml.safe_load(map_path.read_text())
    assert parsed["paths"][target] == "myws"


def test_register_404_exits_2_no_write(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/ghost", method="GET"
    ).respond_with_json({"detail": "not found"}, status=404)
    server_url = _ipv4(httpserver.url_for(""))
    target = str(temp_config_dir["home"] / "repo")
    main_mod = _reload_and_get_app(server_url)
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "register", "ghost", target],
    )
    assert result.exit_code == 2
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    assert not map_path.exists()


def test_register_uses_git_toplevel_when_no_path(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    httpserver.expect_request(
        "/v1/api/workspaces/myws", method="GET"
    ).respond_with_json({"slug": "myws", "status": "active"})
    server_url = _ipv4(httpserver.url_for(""))
    git_root = tmp_path / "myrepo"
    git_root.mkdir()
    main_mod = _reload_and_get_app(server_url)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = str(git_root) + "\n"
        result = cli_runner.invoke(
            main_mod.app,  # type: ignore[attr-defined]
            ["workspace", "register", "myws"],
        )
    assert result.exit_code == 0, result.output
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    parsed = yaml.safe_load(map_path.read_text())
    assert str(git_root) in parsed["paths"]


def test_unregister_removes_entry(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    target = str(temp_config_dir["home"] / "repo")
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    map_path.write_text(yaml.safe_dump({"paths": {target: "myws"}}))
    main_mod = _reload_and_get_app("http://127.0.0.1:1")
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "unregister", target],
    )
    assert result.exit_code == 0, result.output
    parsed = yaml.safe_load(map_path.read_text())
    assert target not in (parsed.get("paths") or {})


def test_unregister_missing_exits_2(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    main_mod = _reload_and_get_app("http://127.0.0.1:1")
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "unregister", "/no/such/path"],
    )
    assert result.exit_code == 2


def test_registrations_prints_table(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    p1 = str(temp_config_dir["home"] / "proj1")
    p2 = str(temp_config_dir["home"] / "proj2")
    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    map_path.write_text(yaml.safe_dump({"paths": {p1: "ws-a", p2: "ws-b"}}))
    main_mod = _reload_and_get_app("http://127.0.0.1:1")
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "registrations"],
    )
    assert result.exit_code == 0, result.output
    assert "ws-a" in result.output
    assert "ws-b" in result.output


def test_registrations_empty_message(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    main_mod = _reload_and_get_app("http://127.0.0.1:1")
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["workspace", "registrations"],
    )
    assert result.exit_code == 0, result.output
    assert "no registrations" in result.output
