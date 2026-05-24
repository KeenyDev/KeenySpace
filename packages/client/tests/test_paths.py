from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_xdg_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    import keenyspace.paths as paths_mod

    reloaded = importlib.reload(paths_mod)

    assert tmp_path / "cfg" / "keenyspace" == reloaded.CONFIG_DIR
    assert tmp_path / "state" / "keenyspace" == reloaded.STATE_DIR
    assert tmp_path / "cfg" / "keenyspace" / "config.yaml" == reloaded.CONFIG_YAML
    assert tmp_path / "cfg" / "keenyspace" / "auth.json" == reloaded.AUTH_JSON
    assert tmp_path / "state" / "keenyspace" / "daemon.sock" == reloaded.DAEMON_SOCK
