"""Hook entry-point tests: stdin envelope -> JSONL UDS -> daemon socket.

We exercise the hook handlers directly (not via subprocess) so we can plug
the conftest mock_daemon fixture into the same event loop. Subprocess
latency is exercised separately by test_hook_latency.py.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import sys
from pathlib import Path
from typing import Any


async def _reload_hooks_modules() -> tuple[Any, Any]:
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    import keenyspace.hooks.dropped as dropped_mod

    importlib.reload(dropped_mod)
    import keenyspace.hooks.uds_client as uds_client_mod

    importlib.reload(uds_client_mod)
    import keenyspace.hooks.handlers as handlers_mod

    importlib.reload(handlers_mod)
    return handlers_mod, paths_mod


def _set_stdin(monkeypatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


async def test_post_tool_fire_and_forget(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
) -> None:
    handlers_mod, _ = await _reload_hooks_modules()
    _set_stdin(monkeypatch, {"cwd": "/tmp", "session_id": "s1"})
    await handlers_mod.handle_post_tool()
    # Daemon has a brief window to receive the envelope; await one yield.
    await asyncio.sleep(0.05)
    received = mock_daemon["received"]
    assert len(received) == 1, f"expected 1 received envelope, got {received}"
    env = received[0]
    assert env["kind"] == "post-tool"
    assert "ts" in env
    assert "payload" in env
    assert env["payload"]["session_id"] == "s1"


async def test_post_tool_daemon_down_exits_clean_and_increments_dropped(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    monkeypatch,
    capsys,
) -> None:
    handlers_mod, paths_mod = await _reload_hooks_modules()
    # mock_daemon fixture NOT requested — socket does not exist.
    assert not paths_mod.DAEMON_SOCK.exists()
    _set_stdin(monkeypatch, {"cwd": "/tmp"})
    await handlers_mod.handle_post_tool()  # must not raise
    err = capsys.readouterr().err
    assert "daemon socket unreachable" in err
    state = json.loads(paths_mod.DROPPED_JSON.read_text())
    assert state["by_kind"]["post-tool"]["count"] == 1


async def test_session_start_compact_request_response(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    monkeypatch,
    capsys,
) -> None:
    """Mock daemon answers with ok=True; hook prints hookSpecificOutput JSON."""
    handlers_mod, paths_mod = await _reload_hooks_modules()
    sock_path = paths_mod.DAEMON_SOCK
    sock_path.parent.mkdir(parents=True, exist_ok=True)

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            assert line, "no line received"
            payload = {
                "ok": True,
                "content": "assembled text",
                "error": None,
            }
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handler, path=str(sock_path))
    try:
        _set_stdin(
            monkeypatch,
            {
                "source": "compact",
                "transcript_path": "/tmp/transcript.jsonl",
                "cwd": "/tmp",
            },
        )
        await handlers_mod.handle_session_start()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert parsed["hookSpecificOutput"]["additionalContext"] == "assembled text"
    finally:
        server.close()
        await server.wait_closed()


async def test_session_start_compact_fail_open_no_stdout(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    monkeypatch,
    capsys,
) -> None:
    """No daemon: hook exits 0 with empty stdout; dropped.json gets compact key."""
    handlers_mod, paths_mod = await _reload_hooks_modules()
    assert not paths_mod.DAEMON_SOCK.exists()
    _set_stdin(monkeypatch, {"source": "compact", "transcript_path": "/tmp/x"})
    await handlers_mod.handle_session_start()
    out = capsys.readouterr().out
    assert out == ""
    state = json.loads(paths_mod.DROPPED_JSON.read_text())
    assert state["by_kind"]["session-start.compact"]["count"] == 1


async def test_session_start_startup_fire_and_forget(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
    capsys,
) -> None:
    handlers_mod, _ = await _reload_hooks_modules()
    _set_stdin(monkeypatch, {"source": "startup", "cwd": "/tmp"})
    await handlers_mod.handle_session_start()
    await asyncio.sleep(0.05)
    out = capsys.readouterr().out
    assert out == ""
    received = mock_daemon["received"]
    assert len(received) == 1
    assert received[0]["kind"] == "session-start"
    assert received[0]["source"] == "startup"


async def test_post_compact_fire_and_forget_no_stdout(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
    capsys,
) -> None:
    """F-09: PostCompact stdout is ignored by Claude Code — never write to stdout."""
    handlers_mod, _ = await _reload_hooks_modules()
    _set_stdin(monkeypatch, {"cwd": "/tmp"})
    await handlers_mod.handle_post_compact()
    await asyncio.sleep(0.05)
    out = capsys.readouterr().out
    assert out == ""
    received = mock_daemon["received"]
    assert len(received) == 1
    assert received[0]["kind"] == "post-compact"


async def test_session_end_fire_and_forget(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
) -> None:
    handlers_mod, _ = await _reload_hooks_modules()
    _set_stdin(monkeypatch, {"cwd": "/tmp"})
    await handlers_mod.handle_session_end()
    await asyncio.sleep(0.05)
    assert len(mock_daemon["received"]) == 1
    assert mock_daemon["received"][0]["kind"] == "session-end"


async def test_pre_compact_fire_and_forget(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
) -> None:
    handlers_mod, _ = await _reload_hooks_modules()
    _set_stdin(monkeypatch, {"cwd": "/tmp"})
    await handlers_mod.handle_pre_compact()
    await asyncio.sleep(0.05)
    assert len(mock_daemon["received"]) == 1
    assert mock_daemon["received"][0]["kind"] == "pre-compact"


async def test_malformed_envelope_exits_clean(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
    capsys,
) -> None:
    handlers_mod, _ = await _reload_hooks_modules()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    await handlers_mod.handle_post_tool()  # must not raise
    await asyncio.sleep(0.05)
    err = capsys.readouterr().err
    assert "malformed" in err
    # Daemon still received an envelope with empty payload
    assert len(mock_daemon["received"]) == 1


async def test_empty_stdin_exits_clean(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
) -> None:
    handlers_mod, _ = await _reload_hooks_modules()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    await handlers_mod.handle_post_tool()
    await asyncio.sleep(0.05)
    assert len(mock_daemon["received"]) == 1


async def test_hook_drops_on_default_workspace(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
    capsys,
) -> None:
    (temp_config_dir["config_dir"] / "config.yaml").write_text("default_workspace: fallback\n")
    import keenyspace.config as cfg_mod
    importlib.reload(cfg_mod)
    cfg_mod.get_client_settings.cache_clear()  # type: ignore[attr-defined]
    import keenyspace.workspace_inference as inf_mod
    importlib.reload(inf_mod)
    handlers_mod, paths_mod = await _reload_hooks_modules()
    _set_stdin(monkeypatch, {"cwd": "/tmp/no-mapping-here", "session_id": "s99"})
    await handlers_mod.handle_post_tool()
    await asyncio.sleep(0.05)
    assert len(mock_daemon["received"]) == 0, "expected event to be dropped"
    err = capsys.readouterr().err
    assert "no workspace mapping" in err
    assert "/tmp/no-mapping-here" in err
    state = json.loads(paths_mod.DROPPED_JSON.read_text())
    assert state["by_kind"]["unmapped-workspace"]["count"] == 1


async def test_hook_forwards_when_mapped(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    mock_daemon: dict[str, Any],
    monkeypatch,
) -> None:
    import yaml

    map_path = temp_config_dir["config_dir"] / "workspace-map.yaml"
    cwd_path = str(Path("/tmp").resolve())
    map_path.write_text(yaml.safe_dump({"paths": {cwd_path: "mapped"}}))
    import keenyspace.config as cfg_mod
    importlib.reload(cfg_mod)
    cfg_mod.get_client_settings.cache_clear()  # type: ignore[attr-defined]
    import keenyspace.workspace_inference as inf_mod
    importlib.reload(inf_mod)
    handlers_mod, _ = await _reload_hooks_modules()
    _set_stdin(monkeypatch, {"cwd": "/tmp", "session_id": "s77"})
    await handlers_mod.handle_post_tool()
    await asyncio.sleep(0.05)
    assert len(mock_daemon["received"]) == 1
    assert mock_daemon["received"][0]["workspace_slug"] == "mapped"
