from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _restore_os_environ() -> Iterator[None]:
    """Snapshot/restore os.environ around every test.

    Several CLI tests set ``os.environ["KEENYSPACE_SERVER_URL"]`` directly (not
    via monkeypatch) inside reload helpers; without restoration that value leaks
    into later tests (e.g. test_config) and breaks their assertions.
    """
    import os

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture
def short_xdg_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Short /tmp-based XDG_STATE_HOME so AF_UNIX sockets bind under macOS 104-byte cap.

    pytest tmp_path is too long for asyncio.start_unix_server on macOS. Tests
    that bind the daemon socket override XDG_STATE_HOME with this fixture;
    config files still live under temp_config_dir.
    """
    short = Path(tempfile.mkdtemp(prefix="ks-d-", dir="/tmp"))
    monkeypatch.setenv("XDG_STATE_HOME", str(short))
    state_dir = short / "keenyspace"
    yield state_dir
    for p in state_dir.glob("*"):
        with contextlib.suppress(OSError):
            p.unlink()
    with contextlib.suppress(OSError):
        state_dir.rmdir()
    with contextlib.suppress(OSError):
        short.rmdir()


@pytest.fixture
def cli_runner() -> Any:
    """Typer CliRunner instance for invoking CLI commands in tests.

    Imported lazily so collection-only runs without typer installed still work.
    """
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def temp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolate XDG dirs under tmp_path; mirrors PROJECT.md ~/.config/keenyspace layout."""

    config_dir = tmp_path / ".config" / "keenyspace"
    state_dir = tmp_path / ".local" / "state" / "keenyspace"
    config_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return {"config_dir": config_dir, "state_dir": state_dir, "home": tmp_path}


@pytest_asyncio.fixture
async def mock_daemon(
    temp_config_dir: dict[str, Path],
) -> AsyncIterator[dict[str, Any]]:
    """Spin up an asyncio.start_unix_server stub that records received envelopes.

    Tests can await envelopes via the returned ``received`` list. Cleans up the
    socket file in teardown.

    Note: resolves the socket path via the paths module so that callers using
    the ``short_xdg_state`` fixture pick up the /tmp-based path (AF_UNIX cap on
    macOS makes pytest tmp_path too long).
    """

    received: list[dict[str, Any]] = []
    # Reload paths so XDG_STATE_HOME override (e.g. from short_xdg_state) flows in.
    import importlib

    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    sock_path = paths_mod.DAEMON_SOCK
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.parent.chmod(0o700)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if line:
                received.append(json.loads(line.decode()))
        finally:
            writer.close()
            try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
                await writer.wait_closed()
            except OSError:
                pass

    server = await asyncio.start_unix_server(handler, path=str(sock_path))
    try:
        yield {"received": received, "sock": sock_path, "server": server}
    finally:
        server.close()
        await server.wait_closed()
        if sock_path.exists():
            sock_path.unlink()


@pytest_asyncio.fixture
async def function_model_agent() -> AsyncIterator[Any]:
    """FunctionModel-based pydantic-ai test double for deterministic outputs.

    Mirrors the Phase 2 eval pattern; tests substitute this for a live LLM call.
    Yields None when pydantic_ai is not importable so collection still succeeds.
    """

    try:
        from pydantic_ai.models.function import FunctionModel
    except ImportError:
        yield None
        return

    async def _fake(_messages: Any, _info: Any) -> Any:
        return None

    yield FunctionModel(_fake)
