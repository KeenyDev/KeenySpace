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


def _set_default_pull_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    pull_root = tmp_path / "keenyspace"
    monkeypatch.setattr(
        "keenyspace.cli.pull.__defaults__", (), raising=False
    )
    monkeypatch.setattr(
        "keenyspace.paths.DEFAULT_PULL_ROOT", pull_root, raising=True
    )
    return pull_root


async def test_pull_clean_succeeds(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)

    server_files_bytes = {
        "index.md": b"# index\n",
        "concepts/foo.md": b"# foo\nbody\n",
        "raw/img.png": b"\x89PNG fake",
    }
    server_manifest = {rel: _sha256(b) for rel, b in server_files_bytes.items()}
    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": server_manifest, "server_canon_at": "2026-05-24T00:00:00Z"}
    )
    for rel, payload in server_files_bytes.items():
        httpserver.expect_request(
            f"/v1/api/workspaces/demo/pages-raw/{rel}"
        ).respond_with_data(payload, content_type="application/octet-stream")

    pull_mod = _reload()
    pull_root = tmp_path / "keenyspace"
    target = pull_root / "demo"
    await pull_mod.run_pull("demo", force=False, target=target)  # type: ignore[attr-defined]

    assert (target / "index.md").read_bytes() == server_files_bytes["index.md"]
    assert (
        target / "concepts" / "foo.md"
    ).read_bytes() == server_files_bytes["concepts/foo.md"]
    assert (
        target / "raw" / "img.png"
    ).read_bytes() == server_files_bytes["raw/img.png"]

    marker_path = target / ".keenyspace" / "slug-marker.json"
    assert marker_path.is_file()
    assert json.loads(marker_path.read_text()) == {"slug": "demo"}
    assert stat.S_IMODE(marker_path.stat().st_mode) == 0o600

    state_path = temp_config_dir["state_dir"] / "demo" / "local-state.json"
    assert state_path.is_file()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    parsed = json.loads(state_path.read_text())
    assert parsed["workspace_slug"] == "demo"
    assert parsed["files"] == server_manifest


async def test_pull_dirty_modified_refuses(
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
    (target / "index.md").write_bytes(b"# server canon\n")
    server_manifest = {"index.md": _sha256(b"# server canon\n")}
    # Mutate local AFTER manifest known.
    (target / "index.md").write_bytes(b"# locally edited\n")

    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": server_manifest, "server_canon_at": "2026-05-24T00:00:00Z"}
    )

    pull_mod = _reload()
    with pytest.raises(SystemExit) as excinfo:
        await pull_mod.run_pull("demo", force=False, target=target)  # type: ignore[attr-defined]
    assert excinfo.value.code == 4


async def test_pull_dirty_added_refuses(
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
    (target / "extra.md").write_bytes(b"new local note\n")

    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": {}, "server_canon_at": "2026-05-24T00:00:00Z"}
    )

    pull_mod = _reload()
    with pytest.raises(SystemExit) as excinfo:
        await pull_mod.run_pull("demo", force=False, target=target)  # type: ignore[attr-defined]
    assert excinfo.value.code == 4


async def test_pull_dirty_removed_refuses(
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
    # Vault is missing 'index.md' but the server lists it.
    server_manifest = {"index.md": _sha256(b"# canon\n")}
    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": server_manifest, "server_canon_at": "2026-05-24T00:00:00Z"}
    )

    pull_mod = _reload()
    with pytest.raises(SystemExit) as excinfo:
        await pull_mod.run_pull("demo", force=False, target=target)  # type: ignore[attr-defined]
    assert excinfo.value.code == 4


async def test_pull_ignores_non_md_outside_raw(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    """Pitfall #9 invariant: notes.txt in vault root MUST NOT trigger dirty."""
    _seed_auth(temp_config_dir["config_dir"])
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)

    target = tmp_path / "keenyspace" / "demo"
    target.mkdir(parents=True)
    (target / "notes.txt").write_bytes(b"arbitrary local non-tracked file\n")

    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": {}, "server_canon_at": "2026-05-24T00:00:00Z"}
    )

    pull_mod = _reload()
    await pull_mod.run_pull("demo", force=False, target=target)  # type: ignore[attr-defined]
    # notes.txt must remain untouched
    assert (target / "notes.txt").is_file()


async def test_pull_ignores_obsidian_and_keenyspace(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    server_url = _ipv4(httpserver.url_for(""))
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", server_url)

    target = tmp_path / "keenyspace" / "demo"
    (target / ".obsidian").mkdir(parents=True)
    (target / ".obsidian" / "workspace.json").write_bytes(b"{}")
    (target / ".keenyspace").mkdir()
    (target / ".keenyspace" / "cache.json").write_bytes(b"{}")

    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": {}, "server_canon_at": "2026-05-24T00:00:00Z"}
    )

    pull_mod = _reload()
    await pull_mod.run_pull("demo", force=False, target=target)  # type: ignore[attr-defined]
    assert (target / ".obsidian" / "workspace.json").is_file()
    # slug-marker.json was written, but the prior cache.json must persist
    assert (target / ".keenyspace" / "cache.json").is_file()


@pytest.mark.parametrize(
    "rel",
    [
        "",
        "/etc/passwd",
        "../escape.md",
        "concepts/../../escape.md",
        "concepts/..",
        "./index.md",
        "concepts//foo.md",
        "concepts/foo.md/",
        "concepts\\..\\escape.md",
        "index\x00.md",
    ],
)
def test_resolve_vault_path_rejects_unsafe_keys(tmp_path: Path, rel: str) -> None:
    from keenyspace.pull.manifest import UnsafeManifestPathError, resolve_vault_path

    with pytest.raises(UnsafeManifestPathError):
        resolve_vault_path(tmp_path / "vault", rel)


def test_resolve_vault_path_rejects_symlink_out_of_vault(tmp_path: Path) -> None:
    from keenyspace.pull.manifest import UnsafeManifestPathError, resolve_vault_path

    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "concepts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeManifestPathError):
        resolve_vault_path(vault, "concepts/foo.md")


def test_resolve_vault_path_accepts_nested_key(tmp_path: Path) -> None:
    from keenyspace.pull.manifest import resolve_vault_path

    vault = tmp_path / "vault"
    assert resolve_vault_path(vault, "raw/sub/img.png") == vault / "raw" / "sub" / "img.png"


async def test_pull_aborts_before_writing_on_unsafe_manifest_key(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", _ipv4(httpserver.url_for("")))

    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {
            "files": {
                "index.md": _sha256(b"# index\n"),
                "../escape.md": _sha256(b"pwned\n"),
            },
            "server_canon_at": "2026-05-24T00:00:00Z",
        }
    )

    pull_mod = _reload()
    target = tmp_path / "keenyspace" / "demo"
    with pytest.raises(SystemExit) as excinfo:
        await pull_mod.run_pull("demo", force=True, target=target)  # type: ignore[attr-defined]

    assert excinfo.value.code == 7
    assert not target.exists()
    assert not (tmp_path / "keenyspace" / "escape.md").exists()
    assert [req.path for req, _resp in httpserver.log] == [
        "/v1/api/workspaces/demo/manifest"
    ]


async def test_pull_skips_fetching_unchanged_files(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    tmp_path: Path,
) -> None:
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", _ipv4(httpserver.url_for("")))

    target = tmp_path / "keenyspace" / "demo"
    (target / "concepts").mkdir(parents=True)
    unchanged = b"# same on both sides\n"
    (target / "index.md").write_bytes(unchanged)
    (target / "concepts" / "foo.md").write_bytes(b"# stale local\n")
    server_foo = b"# fresh server\n"
    server_manifest = {
        "index.md": _sha256(unchanged),
        "concepts/foo.md": _sha256(server_foo),
    }
    httpserver.expect_request(
        "/v1/api/workspaces/demo/manifest"
    ).respond_with_json(
        {"files": server_manifest, "server_canon_at": "2026-05-24T00:00:00Z"}
    )
    httpserver.expect_request(
        "/v1/api/workspaces/demo/pages-raw/concepts/foo.md"
    ).respond_with_data(server_foo, content_type="application/octet-stream")
    inode_before = (target / "index.md").stat().st_ino

    pull_mod = _reload()
    await pull_mod.run_pull("demo", force=True, target=target)  # type: ignore[attr-defined]

    fetched = [req.path for req, _resp in httpserver.log]
    assert fetched.count("/v1/api/workspaces/demo/pages-raw/concepts/foo.md") == 1
    assert "/v1/api/workspaces/demo/pages-raw/index.md" not in fetched
    assert (target / "index.md").stat().st_ino == inode_before
    assert (target / "concepts" / "foo.md").read_bytes() == server_foo
    state_path = temp_config_dir["state_dir"] / "demo" / "local-state.json"
    assert json.loads(state_path.read_text())["files"] == server_manifest
