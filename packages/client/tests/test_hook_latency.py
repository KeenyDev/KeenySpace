"""Hook latency budget — HK-09 (every hook exits <1s p95)."""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


async def _reload() -> tuple[Any, Any]:
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    import keenyspace.hooks.dropped as dropped_mod

    importlib.reload(dropped_mod)
    import keenyspace.hooks.uds_client as uds_client_mod

    importlib.reload(uds_client_mod)
    import keenyspace.hooks.handlers as handlers_mod

    importlib.reload(handlers_mod)
    return handlers_mod, paths_mod


async def test_post_tool_under_p95_300ms(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
) -> None:
    """50 in-process post-tool invocations; p95 must stay under 300ms."""
    handlers_mod, _ = await _reload()

    durations: list[float] = []
    for _ in range(50):
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(json.dumps({"cwd": "/tmp", "session_id": "s"}))
        )
        start = time.perf_counter()
        await handlers_mod.handle_post_tool()
        durations.append(time.perf_counter() - start)
    p95 = statistics.quantiles(durations, n=20)[-1]
    assert p95 < 0.3, f"p95 latency {p95 * 1000:.1f}ms >= 300ms"


async def test_session_start_compact_under_1s(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    monkeypatch,
) -> None:
    """session-start source=compact request-response wall-clock under 1s."""
    handlers_mod, paths_mod = await _reload()
    sock_path = paths_mod.DAEMON_SOCK
    sock_path.parent.mkdir(parents=True, exist_ok=True)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            assert line
            # Respond after a small delay (well under the 800ms budget)
            await asyncio.sleep(0.05)
            payload = {"ok": True, "content": "x" * 1000, "error": None}
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handler, path=str(sock_path))
    try:
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "source": "compact",
                        "transcript_path": "/tmp/x",
                        "cwd": "/tmp",
                    }
                )
            ),
        )
        start = time.perf_counter()
        await handlers_mod.handle_session_start()
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"elapsed {elapsed * 1000:.1f}ms >= 1s"
    finally:
        server.close()
        await server.wait_closed()
