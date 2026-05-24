from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from pydantic import ValidationError


def _reload_config() -> object:
    import keenyspace.paths as paths_mod
    importlib.reload(paths_mod)
    import keenyspace.config as cfg
    return importlib.reload(cfg)


def test_yaml_overrides_defaults(temp_config_dir: dict[str, Path]) -> None:
    config_dir = temp_config_dir["config_dir"]
    (config_dir / "config.yaml").write_text(
        "server_url: http://test\n"
        "llm:\n"
        "  provider: openai\n"
    )
    cfg = _reload_config()
    settings = cfg.ClientSettings()  # type: ignore[attr-defined]
    assert settings.server_url == "http://test"
    assert settings.llm.provider == "openai"
    assert settings.llm.model == "claude-sonnet-4-6"


def test_env_overrides_yaml(
    temp_config_dir: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = temp_config_dir["config_dir"]
    (config_dir / "config.yaml").write_text("server_url: http://from-yaml\n")
    monkeypatch.setenv("KEENYSPACE_SERVER_URL", "http://env")
    cfg = _reload_config()
    settings = cfg.ClientSettings()  # type: ignore[attr-defined]
    assert settings.server_url == "http://env"


def test_missing_server_url_raises(temp_config_dir: dict[str, Path]) -> None:
    cfg = _reload_config()
    with pytest.raises(ValidationError):
        cfg.ClientSettings()  # type: ignore[attr-defined]
