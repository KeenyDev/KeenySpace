"""Kill switch test: ~/.config/keenyspace/disabled forces clean daemon exit."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


async def test_killswitch_present_exits_clean(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    import keenyspace.daemon.server as server_mod

    importlib.reload(server_mod)

    paths_mod.KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
    paths_mod.KILL_SWITCH.write_text("")

    # serve() must return immediately without binding the socket.
    await server_mod.serve()

    assert not paths_mod.DAEMON_SOCK.exists()
    assert not paths_mod.DAEMON_PID.exists()
    out = capsys.readouterr().out
    assert "daemon.killswitch_active" in out
