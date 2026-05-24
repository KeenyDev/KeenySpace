from __future__ import annotations

import importlib
from pathlib import Path

import yaml
from typer.testing import CliRunner


def _reload_and_get_app() -> object:
    import keenyspace.paths as paths_mod
    importlib.reload(paths_mod)
    import keenyspace.config as cfg
    importlib.reload(cfg)
    import keenyspace.cli.init_cmd as init_mod
    importlib.reload(init_mod)
    import keenyspace.__main__ as main_mod
    return importlib.reload(main_mod)


def test_wizard_writes_config(
    temp_config_dir: dict[str, Path], cli_runner: CliRunner
) -> None:
    main_mod = _reload_and_get_app()
    result = cli_runner.invoke(main_mod.app, ["init"], input="https://example.com\n\nn\n")  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    config_path = temp_config_dir["config_dir"] / "config.yaml"
    assert config_path.exists()
    payload = yaml.safe_load(config_path.read_text())
    assert payload == {"server_url": "https://example.com"}


def test_wizard_trims_trailing_slash(
    temp_config_dir: dict[str, Path], cli_runner: CliRunner
) -> None:
    main_mod = _reload_and_get_app()
    result = cli_runner.invoke(main_mod.app, ["init"], input="https://example.com/\n\nn\n")  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    config_path = temp_config_dir["config_dir"] / "config.yaml"
    payload = yaml.safe_load(config_path.read_text())
    assert payload["server_url"] == "https://example.com"


def test_wizard_with_default_workspace(
    temp_config_dir: dict[str, Path], cli_runner: CliRunner
) -> None:
    main_mod = _reload_and_get_app()
    result = cli_runner.invoke(main_mod.app, ["init"], input="http://x\nresearch\nn\n")  # type: ignore[attr-defined]
    assert result.exit_code == 0, result.output
    config_path = temp_config_dir["config_dir"] / "config.yaml"
    payload = yaml.safe_load(config_path.read_text())
    assert payload["default_workspace"] == "research"
    assert payload["server_url"] == "http://x"
