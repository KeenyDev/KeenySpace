"""Service install/uninstall tests + template invariants."""

from __future__ import annotations

import importlib
import json
import platform
import stat
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

# ------------------------- template invariants -----------------------------


def test_templates_packaged() -> None:
    """importlib.resources can read each template from the installed package."""
    plist = files("keenyspace.templates").joinpath(
        "launchd_com.keenyspace.daemon.plist"
    ).read_text(encoding="utf-8")
    assert plist.startswith('<?xml version="1.0"'), "plist must be valid XML"
    assert "KeepAlive" in plist
    assert "__KEENYSPACE_BIN__" in plist
    assert "__HOME__" in plist

    unit = files("keenyspace.templates").joinpath(
        "systemd_keenyspace.service"
    ).read_text(encoding="utf-8")
    assert "[Service]" in unit
    assert "Restart=on-failure" in unit
    assert "__KEENYSPACE_BIN__" in unit

    settings = json.loads(
        files("keenyspace.templates").joinpath("claude-code-settings.json").read_text(
            encoding="utf-8"
        )
    )
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]


def test_claude_code_settings_template_registers_compact_matcher() -> None:
    settings = json.loads(
        files("keenyspace.templates").joinpath("claude-code-settings.json").read_text(
            encoding="utf-8"
        )
    )
    session_start = settings["hooks"]["SessionStart"]
    compact_entries = [e for e in session_start if e.get("matcher") == "compact"]
    assert len(compact_entries) == 1, "exactly one compact matcher expected"
    inner = compact_entries[0]["hooks"][0]
    assert inner["command"] == "keenyspace hook session-start"


@pytest.mark.parametrize(
    ("template_name", "example_path"),
    [
        (
            "launchd_com.keenyspace.daemon.plist",
            "examples/launchd/com.keenyspace.daemon.plist",
        ),
        (
            "systemd_keenyspace.service",
            "examples/systemd/keenyspace.service",
        ),
        (
            "claude-code-settings.json",
            "examples/claude-code-settings.json",
        ),
    ],
)
def test_examples_match_templates(template_name: str, example_path: str) -> None:
    """examples/ files must be byte-equal to the templates bundled in the wheel.

    CI invariant: when one is edited the other must follow. Prevents drift
    between documentation copies and the install-time source of truth.
    """
    template = (
        files("keenyspace.templates").joinpath(template_name).read_text(encoding="utf-8")
    )
    # repo root = three levels up from this test file (tests/.. -> packages/client/.. -> repo)
    repo_root = Path(__file__).resolve().parents[3]
    example = (repo_root / example_path).read_text(encoding="utf-8")
    assert template == example, f"{template_name} differs from {example_path}"


# ------------------------- service install dispatch -----------------------


def test_unsupported_os_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import keenyspace.cli.service as service_mod

    importlib.reload(service_mod)
    monkeypatch.setattr(platform, "system", lambda: "FreeBSD")
    with pytest.raises(typer_exc()):
        service_mod.service_install()


def typer_exc() -> type[BaseException]:
    """Return typer.BadParameter (deferred import — keeps cold-boot tests quiet)."""
    import typer

    return typer.BadParameter


def test_plist_renders_with_resolved_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import keenyspace.cli.service as service_mod

    importlib.reload(service_mod)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Use a real existing path so Path.resolve() does not canonicalise via the
    # macOS firmlink at /System/Volumes/Data.
    fake_bin = tmp_path / "bin" / "keenyspace"
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    fake_bin.touch()
    monkeypatch.setattr(service_mod.shutil, "which", lambda _bin: str(fake_bin))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    captured: list[list[str]] = []

    def fake_run(args: list[str], check: bool = True) -> object:
        captured.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(service_mod.subprocess, "run", fake_run)

    service_mod._install_macos()  # type: ignore[attr-defined]

    plist_dest = (
        tmp_path / "Library" / "LaunchAgents" / "com.keenyspace.daemon.plist"
    )
    content = plist_dest.read_text(encoding="utf-8")
    assert f"<string>{fake_bin.resolve()}</string>" in content
    assert "__KEENYSPACE_BIN__" not in content
    assert "__HOME__" not in content
    # First subprocess invocation must be launchctl bootstrap gui/<uid>
    assert captured[0][0] == "/bin/launchctl"
    assert captured[0][1] == "bootstrap"
    assert captured[0][2].startswith("gui/")
    assert captured[1][1] == "enable"


def test_plist_written_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import keenyspace.cli.service as service_mod

    importlib.reload(service_mod)
    fake_bin = tmp_path / "bin" / "keenyspace"
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    fake_bin.touch()
    monkeypatch.setattr(service_mod.shutil, "which", lambda _bin: str(fake_bin))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        service_mod.subprocess,
        "run",
        lambda args, check=True: subprocess.CompletedProcess(args, 0),
    )
    plist_dest = tmp_path / "Library" / "LaunchAgents" / "com.keenyspace.daemon.plist"
    plist_dest.parent.mkdir(parents=True)
    plist_dest.write_text("stale world-readable copy", encoding="utf-8")
    plist_dest.chmod(0o644)

    service_mod._install_macos()  # type: ignore[attr-defined]

    assert stat.S_IMODE(plist_dest.stat().st_mode) == 0o600
    assert "__KEENYSPACE_BIN__" not in plist_dest.read_text(encoding="utf-8")


def test_systemd_unit_renders_with_resolved_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import keenyspace.cli.service as service_mod

    importlib.reload(service_mod)
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_bin = tmp_path / "bin" / "keenyspace"
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    fake_bin.touch()
    monkeypatch.setattr(service_mod.shutil, "which", lambda _bin: str(fake_bin))
    captured: list[list[str]] = []

    def fake_run(args: list[str], check: bool = True) -> object:
        captured.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(service_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    service_mod._install_linux()  # type: ignore[attr-defined]

    unit_dest = tmp_path / ".config" / "systemd" / "user" / "keenyspace.service"
    content = unit_dest.read_text(encoding="utf-8")
    assert (
        f"ExecStart={fake_bin.resolve()} daemon start --foreground" in content
    )
    assert "__KEENYSPACE_BIN__" not in content
    # Expect daemon-reload then enable --now
    assert captured[0] == ["systemctl", "--user", "daemon-reload"]
    assert captured[1][:4] == ["systemctl", "--user", "enable", "--now"]


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only smoke test")
def test_install_macos_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import keenyspace.cli.service as service_mod

    importlib.reload(service_mod)
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_bin = tmp_path / "bin" / "keenyspace"
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    fake_bin.touch()
    monkeypatch.setattr(service_mod.shutil, "which", lambda _bin: str(fake_bin))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured: list[list[str]] = []
    monkeypatch.setattr(
        service_mod.subprocess,
        "run",
        lambda args, check=True: (
            captured.append(list(args)) or subprocess.CompletedProcess(args, 0)
        ),
    )
    service_mod.service_install()
    assert any("bootstrap" in cmd for cmd in captured)


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux-only smoke test")
def test_install_linux_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import keenyspace.cli.service as service_mod

    importlib.reload(service_mod)
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_bin = tmp_path / "bin" / "keenyspace"
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    fake_bin.touch()
    monkeypatch.setattr(service_mod.shutil, "which", lambda _bin: str(fake_bin))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    captured: list[list[str]] = []
    monkeypatch.setattr(
        service_mod.subprocess,
        "run",
        lambda args, check=True: (
            captured.append(list(args)) or subprocess.CompletedProcess(args, 0)
        ),
    )
    service_mod.service_install()
    assert ["systemctl", "--user", "daemon-reload"] in captured


def test_service_help_lists_install_uninstall() -> None:
    """`keenyspace service --help` lists both subcommands and stays under 600ms."""
    import time

    import keenyspace.__main__ as main_mod

    importlib.reload(main_mod)
    from typer.testing import CliRunner

    start = time.perf_counter()
    runner = CliRunner()
    result = runner.invoke(main_mod.app, ["service", "--help"])
    elapsed = time.perf_counter() - start
    assert result.exit_code == 0
    assert "install" in result.output
    assert "uninstall" in result.output
    assert elapsed < 0.6, f"`service --help` took {elapsed * 1000:.1f}ms"
