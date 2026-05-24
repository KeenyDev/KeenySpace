"""Socket file mode invariants — T-05.05-02 mitigation (info disclosure)."""

from __future__ import annotations

import asyncio
import importlib
import stat
from pathlib import Path


async def test_socket_mode_is_0600_and_parent_dir_is_0700(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
) -> None:
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    import keenyspace.daemon.server as server_mod

    importlib.reload(server_mod)

    task = asyncio.create_task(server_mod.serve())
    try:
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            if paths_mod.DAEMON_SOCK.exists():
                break
            await asyncio.sleep(0.05)
        assert paths_mod.DAEMON_SOCK.exists(), "daemon did not bind socket"

        sock_mode = stat.S_IMODE(paths_mod.DAEMON_SOCK.stat().st_mode)
        assert sock_mode == 0o600, f"socket mode {oct(sock_mode)} != 0600"

        parent_mode = stat.S_IMODE(paths_mod.DAEMON_SOCK.parent.stat().st_mode)
        assert parent_mode == 0o700, f"parent dir mode {oct(parent_mode)} != 0700"
    finally:
        task.cancel()
        try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
            await task
        except (asyncio.CancelledError, SystemExit):
            pass
        paths_mod.DAEMON_SOCK.unlink(missing_ok=True)
        paths_mod.DAEMON_PID.unlink(missing_ok=True)
