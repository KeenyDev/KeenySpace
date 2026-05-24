from __future__ import annotations

import hashlib
import importlib
import json
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
    import keenyspace.clients.http as http_mod
    importlib.reload(http_mod)
    import keenyspace.pull.manifest as mf
    importlib.reload(mf)
    import keenyspace.pull.stash as st
    importlib.reload(st)
    import keenyspace.cli.pull as pull_mod
    return importlib.reload(pull_mod)


def _ipv4(url: str) -> str:
    return url.replace("localhost", "127.0.0.1")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _seed_auth(config_dir: Path) -> None:
    import os as _os

    auth = config_dir / "auth.json"
    auth.write_text(json.dumps({"api_key": "ks_live_testkey"}))
    _os.chmod(auth, stat.S_IRUSR | stat.S_IWUSR)


async def test_force_stashes_modified_files_with_unified_diff(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)

    target = tmp_path / "keenyspace" / "demo"
    (target / "concepts").mkdir(parents=True)
    server_body = b"# server canon\nthird line\n"
    local_body = b"# locally edited\nfourth line\n"
    (target / "concepts" / "foo.md").write_bytes(local_body)

    server_manifest = {"concepts/foo.md": _sha256(server_body)}
    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": server_manifest, "server_canon_at": "2026-05-24T00:00:00Z"}
    )
    httpserver.expect_request(
        "/v1/api/workspaces/demo/pages-raw/concepts/foo.md"
    ).respond_with_data(server_body, content_type="application/octet-stream")

    pull_mod = _reload()
    await pull_mod.run_pull("demo", force=True, target=target)  # type: ignore[attr-defined]

    state_dir = temp_config_dir["state_dir"] / "demo"
    conflicts = list((state_dir / "conflicts").iterdir())
    assert len(conflicts) == 1
    stashed = conflicts[0] / "concepts" / "foo.md"
    assert stashed.is_file()
    assert stashed.read_bytes() == local_body

    captured = capsys.readouterr().out
    assert "local/concepts/foo.md" in captured
    assert "server/concepts/foo.md" in captured


async def test_force_applies_server_canon(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)

    target = tmp_path / "keenyspace" / "demo"
    (target / "concepts").mkdir(parents=True)
    server_body = b"# fresh server bytes\n"
    (target / "concepts" / "foo.md").write_bytes(b"# stale local bytes\n")

    server_manifest = {"concepts/foo.md": _sha256(server_body)}
    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": server_manifest, "server_canon_at": "2026-05-24T00:00:00Z"}
    )
    httpserver.expect_request(
        "/v1/api/workspaces/demo/pages-raw/concepts/foo.md"
    ).respond_with_data(server_body, content_type="application/octet-stream")

    pull_mod = _reload()
    await pull_mod.run_pull("demo", force=True, target=target)  # type: ignore[attr-defined]

    assert (target / "concepts" / "foo.md").read_bytes() == server_body


async def test_force_does_not_erase_out_of_scope_files(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    """Local notes.txt + .obsidian/* are out-of-scope; --force must not remove them."""
    _seed_auth(temp_config_dir["config_dir"])
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)

    target = tmp_path / "keenyspace" / "demo"
    target.mkdir(parents=True)
    (target / "notes.txt").write_bytes(b"sidecar")
    (target / ".obsidian").mkdir()
    (target / ".obsidian" / "workspace.json").write_bytes(b"{}")
    (target / "gone.md").write_bytes(b"# local-only md\n")  # in scope; not on server

    server_manifest: dict[str, str] = {}
    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": server_manifest, "server_canon_at": "2026-05-24T00:00:00Z"}
    )

    pull_mod = _reload()
    await pull_mod.run_pull("demo", force=True, target=target)  # type: ignore[attr-defined]

    # In-scope local file that disappeared from server canon is removed.
    assert not (target / "gone.md").exists()
    # Out-of-scope files survive.
    assert (target / "notes.txt").is_file()
    assert (target / ".obsidian" / "workspace.json").is_file()


async def test_force_writes_slug_marker_and_local_state(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)

    target = tmp_path / "keenyspace" / "demo"
    target.mkdir(parents=True)
    server_body = b"# only file\n"
    server_manifest = {"index.md": _sha256(server_body)}
    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": server_manifest, "server_canon_at": "2026-05-24T00:00:00Z"}
    )
    httpserver.expect_request(
        "/v1/api/workspaces/demo/pages-raw/index.md"
    ).respond_with_data(server_body, content_type="application/octet-stream")

    pull_mod = _reload()
    await pull_mod.run_pull("demo", force=True, target=target)  # type: ignore[attr-defined]

    marker = target / ".keenyspace" / "slug-marker.json"
    assert marker.is_file()
    assert json.loads(marker.read_text()) == {"slug": "demo"}
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    state_path = temp_config_dir["state_dir"] / "demo" / "local-state.json"
    assert state_path.is_file()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    parsed = json.loads(state_path.read_text())
    assert parsed["workspace_slug"] == "demo"
    assert parsed["files"] == server_manifest
