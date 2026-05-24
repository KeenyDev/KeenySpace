"""Daemon lifecycle tests: serve() binds socket+pidfile, stop/status work end-to-end."""

from __future__ import annotations

import asyncio
import importlib
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import pytest


def _reload(temp_config_dir: dict[str, Path]) -> Iterable[object]:
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    import keenyspace.daemon.server as server_mod

    importlib.reload(server_mod)
    import keenyspace.daemon.cli as cli_mod

    importlib.reload(cli_mod)
    return paths_mod, server_mod, cli_mod


async def test_daemon_start_creates_socket_and_pidfile(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
) -> None:
    paths_mod, server_mod, _ = _reload(temp_config_dir)
    task = asyncio.create_task(server_mod.serve())
    try:
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            if paths_mod.DAEMON_SOCK.exists() and paths_mod.DAEMON_PID.exists():
                break
            await asyncio.sleep(0.05)
        assert paths_mod.DAEMON_SOCK.exists()
        assert paths_mod.DAEMON_PID.exists()
        mode = stat.S_IMODE(paths_mod.DAEMON_SOCK.stat().st_mode)
        assert mode == 0o600, f"socket mode {oct(mode)} != 0600"
        pid = int(paths_mod.DAEMON_PID.read_text().strip())
        assert pid == os.getpid()
    finally:
        task.cancel()
        try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
            await task
        except (asyncio.CancelledError, SystemExit):
            pass
        paths_mod.DAEMON_SOCK.unlink(missing_ok=True)
        paths_mod.DAEMON_PID.unlink(missing_ok=True)


def test_daemon_status_not_running(
    temp_config_dir: dict[str, Path],
    cli_runner,
) -> None:
    _reload(temp_config_dir)
    import keenyspace.__main__ as main_mod

    importlib.reload(main_mod)
    result = cli_runner.invoke(main_mod.app, ["daemon", "status"])
    assert result.exit_code == 1
    assert "not running" in result.output


def test_daemon_status_stale_pidfile(
    temp_config_dir: dict[str, Path],
    cli_runner,
) -> None:
    paths_mod, _, _ = _reload(temp_config_dir)
    paths_mod.DAEMON_PID.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.DAEMON_PID.write_text("999999")
    paths_mod.DAEMON_SOCK.touch()
    import keenyspace.__main__ as main_mod

    importlib.reload(main_mod)
    result = cli_runner.invoke(main_mod.app, ["daemon", "status"])
    assert result.exit_code == 1
    assert "stale" in result.output


def test_daemon_stop_no_pidfile(
    temp_config_dir: dict[str, Path],
    cli_runner,
) -> None:
    _reload(temp_config_dir)
    import keenyspace.__main__ as main_mod

    importlib.reload(main_mod)
    result = cli_runner.invoke(main_mod.app, ["daemon", "stop"])
    assert result.exit_code == 1
    assert "not running" in result.output


def test_daemon_stop_terminates_running_daemon(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
) -> None:
    """Spawn `keenyspace daemon start --foreground` as a subprocess, SIGTERM it."""
    paths_mod, _, _ = _reload(temp_config_dir)
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(temp_config_dir["home"] / ".config")
    env["XDG_STATE_HOME"] = str(short_xdg_state.parent)
    env["HOME"] = str(temp_config_dir["home"])
    proc = subprocess.Popen(
        [sys.executable, "-m", "keenyspace", "daemon", "start", "--foreground"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if paths_mod.DAEMON_SOCK.exists() and paths_mod.DAEMON_PID.exists():
                break
            time.sleep(0.1)
        else:
            proc.terminate()
            out, err = proc.communicate(timeout=2)
            pytest.fail(
                f"daemon did not bind socket in 5s\n"
                f"stdout: {out.decode()}\nstderr: {err.decode()}"
            )
        pid = int(paths_mod.DAEMON_PID.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            if not paths_mod.DAEMON_SOCK.exists():
                break
        else:
            pytest.fail("socket did not vanish within 5s of SIGTERM")
        assert not paths_mod.DAEMON_PID.exists()
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
