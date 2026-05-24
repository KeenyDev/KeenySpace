"""End-to-end daemon UDS smoke tests.

We exercise the daemon server's _handle path in-process (no subprocess —
subprocess monkeypatching is messy and the daemon server logic is the
same). The KEENYSPACE_TEST_AGENT_RESPONSE test hatch bypasses real LLM
calls so we exercise the full request-response cycle deterministically.

Latency assertion: the request-response round-trip must complete under
800ms; this matches the hook's request_response timeout budget.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from pathlib import Path
from typing import Any

import pytest


async def _send_envelope(sock_path: Path, envelope: dict[str, Any]) -> dict[str, Any] | None:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(json.dumps(envelope).encode() + b"\n")
        await writer.drain()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        except TimeoutError:
            return None
        if not line:
            return None
        return json.loads(line.decode())
    finally:
        writer.close()
        try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
            await writer.wait_closed()
        except OSError:
            pass


@pytest.mark.asyncio
async def test_daemon_uds_smoke_session_start_compact(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: spawn daemon server, send session-start source=compact,
    receive a well-formed JSONL response within 800ms."""
    monkeypatch.setenv("KEENYSPACE_TEST_AGENT_RESPONSE", "smoke-test-context")
    # Reload paths so XDG_STATE_HOME override (/tmp-based) flows in.
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    sock_path = paths_mod.DAEMON_SOCK
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.parent.chmod(0o700)

    # Reload server/handlers so the freshly-reloaded paths are honoured.
    import keenyspace.daemon.server as server_mod

    importlib.reload(server_mod)
    import keenyspace.daemon.handlers as handlers_mod

    importlib.reload(handlers_mod)

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"role":"user","content":"hello"}\n' * 5, encoding="utf-8"
    )

    server = await asyncio.start_unix_server(
        server_mod._handle, path=str(sock_path)
    )
    try:
        envelope = {
            "kind": "session-start",
            "source": "compact",
            "workspace_slug": "demo",
            "transcript_path": str(transcript),
        }
        t0 = time.monotonic()
        response = await _send_envelope(sock_path, envelope)
        rtt_ms = (time.monotonic() - t0) * 1000.0
        assert response is not None, "no response from daemon"
        assert set(response.keys()) == {"ok", "content", "error"}
        assert response["ok"] is True
        assert response["content"] == "smoke-test-context"
        assert response["error"] is None
        assert rtt_ms < 800.0, f"RTT {rtt_ms:.1f}ms exceeds 800ms budget"
    finally:
        server.close()
        await server.wait_closed()
        if sock_path.exists():
            sock_path.unlink()


@pytest.mark.asyncio
async def test_daemon_uds_smoke_post_tool_fire_and_forget(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for Plan 05 invariants: post-tool stays fire-and-forget,
    daemon does not write a response back."""
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    sock_path = paths_mod.DAEMON_SOCK
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.parent.chmod(0o700)

    import keenyspace.daemon.server as server_mod

    importlib.reload(server_mod)
    import keenyspace.daemon.handlers as handlers_mod

    importlib.reload(handlers_mod)

    server = await asyncio.start_unix_server(
        server_mod._handle, path=str(sock_path)
    )
    try:
        envelope = {"kind": "post-tool", "workspace_slug": "demo"}
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            writer.write(json.dumps(envelope).encode() + b"\n")
            await writer.drain()
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=0.3)
            except TimeoutError:
                line = b""
            # post-tool stays fire-and-forget; daemon never writes a JSONL line.
            assert line == b""
        finally:
            writer.close()
            try:  # noqa: SIM105
                await writer.wait_closed()
            except OSError:
                pass
    finally:
        server.close()
        await server.wait_closed()
        if sock_path.exists():
            sock_path.unlink()


@pytest.mark.asyncio
async def test_daemon_uds_smoke_missing_transcript_returns_ok_false(
    temp_config_dir: dict[str, Path],
    short_xdg_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If transcript_path is missing/unreadable, daemon still responds with
    {ok=False, error='transcript_unavailable'} so the hook degrades gracefully."""
    # NB: test hatch NOT set; default code path will read transcript and abort
    # before any LLM call because read_transcript_excerpt returns None.
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    sock_path = paths_mod.DAEMON_SOCK
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.parent.chmod(0o700)

    import keenyspace.daemon.server as server_mod

    importlib.reload(server_mod)
    import keenyspace.daemon.handlers as handlers_mod

    importlib.reload(handlers_mod)

    server = await asyncio.start_unix_server(
        server_mod._handle, path=str(sock_path)
    )
    try:
        envelope = {
            "kind": "session-start",
            "source": "compact",
            "workspace_slug": "demo",
            "transcript_path": str(tmp_path / "does_not_exist.jsonl"),
        }
        response = await _send_envelope(sock_path, envelope)
        assert response is not None
        assert response["ok"] is False
        assert response["error"] == "transcript_unavailable"
        assert response["content"] is None
    finally:
        server.close()
        await server.wait_closed()
        if sock_path.exists():
            sock_path.unlink()
