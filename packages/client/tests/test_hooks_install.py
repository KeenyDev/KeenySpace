"""Tests for keenyspace hooks install/uninstall/status subcommands."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

from typer.testing import CliRunner


def _reload_and_get_app() -> object:
    os.environ["COLUMNS"] = "200"
    import keenyspace.cli.hooks as hooks_mod

    importlib.reload(hooks_mod)
    import keenyspace.__main__ as main_mod

    importlib.reload(main_mod)
    return main_mod


def test_install_creates_file_when_absent(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    main_mod = _reload_and_get_app()
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "install"],
    )
    assert result.exit_code == 0, result.output

    settings_path = temp_config_dir["home"] / ".claude" / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    hooks = data["hooks"]

    from keenyspace.cli.hooks import KEENYSPACE_HOOKS

    for event in KEENYSPACE_HOOKS:
        assert event in hooks, f"missing event {event}"

    session_start_commands = [
        obj["command"]
        for g in hooks["SessionStart"]
        for obj in g.get("hooks", [])
    ]
    assert len(hooks["SessionStart"]) == 2
    assert all(cmd.startswith("keenyspace hook ") for cmd in session_start_commands)


def test_install_preserves_foreign_hooks_and_other_keys(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    settings_path = temp_config_dir["home"] / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_group = {
        "hooks": [{"type": "command", "command": "bash /x/gsd-session-state.sh"}]
    }
    initial: dict[str, object] = {
        "hooks": {"SessionStart": [foreign_group]},
        "env": {"FOO": "bar"},
        "permissions": {"allow": ["*"]},
        "model": "claude-opus-4",
        "statusLine": "custom",
    }
    settings_path.write_text(json.dumps(initial))

    main_mod = _reload_and_get_app()
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "install"],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(settings_path.read_text())
    session_start = data["hooks"]["SessionStart"]
    foreign_present = any(
        any(
            obj.get("command") == "bash /x/gsd-session-state.sh"
            for obj in g.get("hooks", [])
        )
        for g in session_start
    )
    assert foreign_present

    ours_present = any(
        any(
            obj.get("command", "").startswith("keenyspace hook ")
            for obj in g.get("hooks", [])
        )
        for g in session_start
    )
    assert ours_present

    assert data["env"] == {"FOO": "bar"}
    assert data["permissions"] == {"allow": ["*"]}
    assert data["model"] == "claude-opus-4"
    assert data["statusLine"] == "custom"


def test_install_idempotent_no_duplicates(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    main_mod = _reload_and_get_app()

    for _ in range(2):
        result = cli_runner.invoke(
            main_mod.app,  # type: ignore[attr-defined]
            ["hooks", "install"],
        )
        assert result.exit_code == 0, result.output

    settings_path = temp_config_dir["home"] / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    session_start = data["hooks"]["SessionStart"]

    from keenyspace.cli.hooks import OURS_PREFIX

    ours_count = sum(
        1
        for g in session_start
        if any(
            obj.get("command", "").startswith(OURS_PREFIX)
            for obj in g.get("hooks", [])
        )
    )
    assert ours_count == 2


def test_install_upgrades_stale_ours_entry(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    settings_path = temp_config_dir["home"] / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    stale_group = {
        "matcher": "compact",
        "hooks": [{"type": "command", "command": "keenyspace hook session-start"}],
    }
    settings_path.write_text(
        json.dumps({"hooks": {"SessionStart": [stale_group]}})
    )

    main_mod = _reload_and_get_app()
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "install"],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(settings_path.read_text())
    from keenyspace.cli.hooks import KEENYSPACE_HOOKS

    canonical_compact = KEENYSPACE_HOOKS["SessionStart"][0]
    compact_groups = [
        g for g in data["hooks"]["SessionStart"] if g.get("matcher") == "compact"
    ]
    assert len(compact_groups) == 1
    assert compact_groups[0] == canonical_compact


def test_uninstall_removes_only_ours(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    settings_path = temp_config_dir["home"] / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_group = {
        "hooks": [{"type": "command", "command": "bash /x/gsd-session-state.sh"}]
    }
    settings_path.write_text(
        json.dumps({"hooks": {"SessionStart": [foreign_group]}})
    )

    main_mod = _reload_and_get_app()
    cli_runner.invoke(main_mod.app, ["hooks", "install"])  # type: ignore[attr-defined]
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "uninstall"],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(settings_path.read_text())
    session_start = data.get("hooks", {}).get("SessionStart", [])
    assert len(session_start) == 1
    cmd = session_start[0]["hooks"][0]["command"]
    assert cmd == "bash /x/gsd-session-state.sh"

    assert "PreCompact" not in data.get("hooks", {})
    assert "PostCompact" not in data.get("hooks", {})
    assert "PostToolUse" not in data.get("hooks", {})
    assert "SessionEnd" not in data.get("hooks", {})


def test_uninstall_preserves_other_top_level_keys(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    settings_path = temp_config_dir["home"] / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"env": {"A": "1"}, "model": "opus"}))

    main_mod = _reload_and_get_app()
    cli_runner.invoke(main_mod.app, ["hooks", "install"])  # type: ignore[attr-defined]
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "uninstall"],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(settings_path.read_text())
    assert data["env"] == {"A": "1"}
    assert data["model"] == "opus"
    assert "hooks" not in data


def test_corrupt_json_refused(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    settings_path = temp_config_dir["home"] / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_content = b"{ not json"
    settings_path.write_bytes(corrupt_content)

    main_mod = _reload_and_get_app()
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "install"],
    )
    assert result.exit_code != 0
    assert settings_path.read_bytes() == corrupt_content


def test_status_installed_not_partial(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    main_mod = _reload_and_get_app()

    cli_runner.invoke(main_mod.app, ["hooks", "install"])  # type: ignore[attr-defined]
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "status"],
    )
    assert result.exit_code == 0, result.output
    assert "SessionStart: installed" in result.output

    settings_path = temp_config_dir["home"] / ".claude" / "settings.json"
    settings_path.unlink()
    result2 = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "status"],
    )
    assert result2.exit_code == 0, result2.output
    assert "SessionEnd: not installed" in result2.output

    from keenyspace.cli.hooks import KEENYSPACE_HOOKS

    partial_settings = {
        "hooks": {"SessionStart": [KEENYSPACE_HOOKS["SessionStart"][0]]}
    }
    settings_path.write_text(json.dumps(partial_settings))
    result3 = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "status"],
    )
    assert result3.exit_code == 0, result3.output
    assert "SessionStart: partial" in result3.output


def test_project_targeting(
    temp_config_dir: dict[str, Path],
    cli_runner: CliRunner,
) -> None:
    proj_dir = temp_config_dir["home"] / "myproject"
    proj_dir.mkdir(parents=True, exist_ok=True)

    main_mod = _reload_and_get_app()
    result = cli_runner.invoke(
        main_mod.app,  # type: ignore[attr-defined]
        ["hooks", "install", "--project", str(proj_dir)],
    )
    assert result.exit_code == 0, result.output

    project_settings = proj_dir / ".claude" / "settings.json"
    assert project_settings.exists()

    global_settings = temp_config_dir["home"] / ".claude" / "settings.json"
    assert not global_settings.exists()

    data = json.loads(project_settings.read_text())
    assert "hooks" in data
