from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _reload() -> object:
    import keenyspace.paths as paths_mod
    importlib.reload(paths_mod)
    import keenyspace.config as cfg
    importlib.reload(cfg)
    cfg.get_client_settings.cache_clear()  # type: ignore[attr-defined]
    import keenyspace.workspace_inference as inf
    return importlib.reload(inf)


def test_env_var_wins(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KEENYSPACE_WORKSPACE", "alpha")
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", "http://srv")
    inf = _reload()
    slug, source = inf.resolve_workspace_slug(cwd=tmp_path)  # type: ignore[attr-defined]
    assert slug == "alpha"
    assert source == "env"


def test_slug_marker_walk_up(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", "http://srv")
    monkeypatch.delenv("KEENYSPACE_WORKSPACE", raising=False)
    vault = tmp_path / "foo" / "bar"
    (vault / ".keenyspace").mkdir(parents=True)
    (vault / ".keenyspace" / "slug-marker.json").write_text(
        json.dumps({"slug": "beta"})
    )
    deep = vault / "concepts"
    deep.mkdir()
    inf = _reload()
    slug, source = inf.resolve_workspace_slug(cwd=deep)  # type: ignore[attr-defined]
    assert slug == "beta"
    assert source == "slug-marker"


def test_workspace_map_longest_prefix(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", "http://srv")
    monkeypatch.delenv("KEENYSPACE_WORKSPACE", raising=False)
    config_dir = temp_config_dir["config_dir"]
    short_prefix = tmp_path / "ws"
    long_prefix = tmp_path / "ws" / "deep"
    long_prefix.mkdir(parents=True)
    (config_dir / "workspace-map.yaml").write_text(
        "paths:\n"
        f"  '{short_prefix}': delta\n"
        f"  '{long_prefix}': gamma\n"
    )
    inf = _reload()
    target = long_prefix / "subdir"
    target.mkdir()
    slug, source = inf.resolve_workspace_slug(cwd=target)  # type: ignore[attr-defined]
    assert slug == "gamma"
    assert source == "workspace-map"


def test_default_fallback(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", "http://srv")
    monkeypatch.delenv("KEENYSPACE_WORKSPACE", raising=False)
    config_dir = temp_config_dir["config_dir"]
    (config_dir / "config.yaml").write_text(
        "server_url: http://srv\ndefault_workspace: epsilon\n"
    )
    inf = _reload()
    slug, source = inf.resolve_workspace_slug(cwd=tmp_path)  # type: ignore[attr-defined]
    assert slug == "epsilon"
    assert source == "default"


def test_unresolved(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", "http://srv")
    monkeypatch.delenv("KEENYSPACE_WORKSPACE", raising=False)
    inf = _reload()
    slug, source = inf.resolve_workspace_slug(cwd=tmp_path)  # type: ignore[attr-defined]
    assert slug is None
    assert source == "unresolved"


def test_explicit_arg(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KEENYSPACE_WORKSPACE", "ignored")
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", "http://srv")
    inf = _reload()
    slug, source = inf.resolve_workspace_slug(cwd=tmp_path, explicit="zeta")  # type: ignore[attr-defined]
    assert slug == "zeta"
    assert source == "explicit"
