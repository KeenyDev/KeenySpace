"""Client `keenyspace restore` multipart upload tests."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

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
    import keenyspace.cli.restore as restore_mod

    importlib.reload(restore_mod)
    import keenyspace.__main__ as main_mod

    return importlib.reload(main_mod)


def _seed_auth(config_dir: Path, *, api_key: str = "ks_live_test") -> None:
    import stat

    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"api_key": api_key}))
    os.chmod(auth_path, stat.S_IRUSR | stat.S_IWUSR)


def _write_archive(tmp_path: Path) -> Path:
    p = tmp_path / "fake.tar.gz"
    p.write_bytes(b"\x1f\x8b\x08\x00fake")
    return p


def test_restore_uploads_multipart(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: Any) -> Any:
        from werkzeug.wrappers import Response

        captured["query"] = request.args.to_dict()
        captured["content_type"] = request.headers.get("Content-Type", "")
        captured["body_len"] = len(request.get_data())
        return Response(json.dumps({"ok": True, "workspaces_restored": 0}), status=200)

    httpserver.expect_request("/v1/admin/restore", method="POST").respond_with_handler(handler)
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    archive = _write_archive(tmp_path)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["restore", str(archive)],
    )
    assert result.exit_code == 0, result.output
    assert "multipart/form-data" in captured["content_type"]
    assert captured["query"].get("force") == "false"
    assert captured["body_len"] > 0


def test_restore_force_passes_query_param(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: Any) -> Any:
        from werkzeug.wrappers import Response

        captured["query"] = request.args.to_dict()
        return Response(json.dumps({"ok": True}), status=200)

    httpserver.expect_request("/v1/admin/restore", method="POST").respond_with_handler(handler)
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    archive = _write_archive(tmp_path)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["restore", str(archive), "--force"],
    )
    assert result.exit_code == 0, result.output
    assert captured["query"].get("force") == "true"


def test_restore_handles_422_version_mismatch(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    httpserver.expect_request("/v1/admin/restore", method="POST").respond_with_json(
        {"detail": {"error": "version_mismatch", "source": "0.2.0", "target": "0.1.0"}},
        status=422,
    )
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    archive = _write_archive(tmp_path)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["restore", str(archive)],
    )
    assert result.exit_code == 6, result.output
    assert "Restore refused" in result.output
    assert "version_mismatch" in result.output


def test_restore_handles_409_target_not_empty(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    httpserver.expect_request("/v1/admin/restore", method="POST").respond_with_json(
        {"detail": {"error": "target_not_empty", "existing_workspaces": 3}}, status=409
    )
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    archive = _write_archive(tmp_path)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["restore", str(archive)],
    )
    assert result.exit_code == 6, result.output
    assert "target_not_empty" in result.output


def test_restore_happy_path(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    httpserver.expect_request("/v1/admin/restore", method="POST").respond_with_json(
        {"ok": True, "workspaces_restored": 3, "wiped": False}, status=200
    )
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    archive = _write_archive(tmp_path)
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["restore", str(archive)],
    )
    assert result.exit_code == 0, result.output


def test_restore_archive_not_found_exits_nonzero(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    server_url = httpserver.url_for("").replace("localhost", "127.0.0.1")
    main_mod = _reload_and_get_app(server_url)
    # Typer Path(exists=True) catches this before our code runs; exit !=0.
    missing = tmp_path / "does-not-exist.tar.gz"
    result = cli_runner.invoke(  # type: ignore[attr-defined]
        main_mod.app,  # type: ignore[attr-defined]
        ["restore", str(missing)],
    )
    assert result.exit_code != 0
