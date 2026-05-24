from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio


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
        try:
            p.unlink()
        except OSError:
            pass
    try:
        state_dir.rmdir()
    except OSError:
        pass
    try:
        short.rmdir()
    except OSError:
        pass


@pytest.fixture
def cli_runner() -> Any:
    """Typer CliRunner instance for invoking CLI commands in tests.

    Imported lazily so collection-only runs without typer installed still work.
    """
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def temp_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
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
    """

    received: list[dict[str, Any]] = []
    sock_path = temp_config_dir["state_dir"] / "daemon.sock"

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
