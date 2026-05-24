from __future__ import annotations

import importlib
import json
import os
import stat
from pathlib import Path

import pytest


def _reload_auth() -> object:
    import keenyspace.paths as paths_mod
    importlib.reload(paths_mod)
    import keenyspace.auth as auth_mod
    return importlib.reload(auth_mod)


def test_loose_mode_refused(temp_config_dir: dict[str, Path]) -> None:
    auth = _reload_auth()
    path = temp_config_dir["config_dir"] / "auth.json"
    path.write_text(json.dumps({"api_key": "ks_live_x"}))
    os.chmod(path, 0o644)
    with pytest.raises(SystemExit) as excinfo:
        auth._validate_auth_file_mode(path)  # type: ignore[attr-defined]
    msg = str(excinfo.value)
    assert "0600" in msg
    assert str(path) in msg


def test_mode_0600_passes(temp_config_dir: dict[str, Path]) -> None:
    auth = _reload_auth()
    path = temp_config_dir["config_dir"] / "auth.json"
    path.write_text("{}")
    os.chmod(path, 0o600)
    auth._validate_auth_file_mode(path)  # type: ignore[attr-defined]


def test_missing_file_passes(temp_config_dir: dict[str, Path]) -> None:
    auth = _reload_auth()
    path = temp_config_dir["config_dir"] / "auth.json"
    assert not path.exists()
    auth._validate_auth_file_mode(path)  # type: ignore[attr-defined]


def test_windows_skips_check(
    temp_config_dir: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = _reload_auth()
    path = temp_config_dir["config_dir"] / "auth.json"
    path.write_text("{}")
    os.chmod(path, 0o644)
    monkeypatch.setattr("sys.platform", "win32")
    auth._validate_auth_file_mode(path)  # type: ignore[attr-defined]


def test_write_auth_creates_mode_0600(temp_config_dir: dict[str, Path]) -> None:
    auth = _reload_auth()
    path = temp_config_dir["config_dir"] / "auth.json"
    auth.write_auth({"api_key": "ks_live_test"}, path)  # type: ignore[attr-defined]
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert json.loads(path.read_text()) == {"api_key": "ks_live_test"}
