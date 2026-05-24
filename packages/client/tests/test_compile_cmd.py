"""Tests for cli/compile_cmd.py — MCP compile trigger + --wait polling state machine."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest


def _seed_auth(config_dir: Path) -> None:
    auth = config_dir / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"api_key": "ks_live_test"}))
    os.chmod(auth, stat.S_IRUSR | stat.S_IWUSR)


def _seed_config(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "\n".join(
            [
                "server_url: http://localhost:8000",
                "default_workspace: demo",
                "llm:",
                "  provider: anthropic",
                "  model: claude-sonnet-4-6",
                "  api_key_env: ANTHROPIC_API_KEY",
                "  timeout_seconds: 120",
            ]
        )
        + "\n"
    )


def _reload() -> Any:
    import importlib

    import keenyspace.auth as auth_mod
    import keenyspace.config as config_mod
    import keenyspace.paths as paths_mod
    import keenyspace.workspace_inference as wi_mod

    importlib.reload(paths_mod)
    importlib.reload(config_mod)
    importlib.reload(auth_mod)
    importlib.reload(wi_mod)
    config_mod.get_client_settings.cache_clear()
    import keenyspace.cli.compile_cmd as compile_mod
    import keenyspace.clients.mcp as mcp_mod

    importlib.reload(mcp_mod)
    importlib.reload(compile_mod)
    return compile_mod


async def test_compile_triggers_mcp_compile_tool(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("COLUMNS", "400")
    mod = _reload()

    captured: dict[str, Any] = {}

    async def _fake_compile(server_url: str, api_key: str, *, workspace: str) -> dict[str, Any]:
        captured["compile"] = workspace
        return {"job_id": "j1", "status": "queued"}

    async def _fake_status(*args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("should not poll without --wait")

    monkeypatch.setattr(mod, "call_compile", _fake_compile)
    monkeypatch.setattr(mod, "call_compile_status", _fake_status)
    await mod.run_compile_cmd(None, wait=False)
    assert captured["compile"] == "demo"
    out = capsys.readouterr().out
    assert "job_id=j1" in out


async def test_compile_wait_polls_until_idle(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("COLUMNS", "400")
    mod = _reload()

    sequence = [
        {"state": "running", "last_compile_at": "t1"},
        {"state": "running", "last_compile_at": "t2"},
        {"state": "idle", "last_compile_at": "t3"},
    ]

    async def _fake_compile(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"job_id": "j1", "status": "queued"}

    async def _fake_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return sequence.pop(0)

    monkeypatch.setattr(mod, "call_compile", _fake_compile)
    monkeypatch.setattr(mod, "call_compile_status", _fake_status)
    await mod.run_compile_cmd(None, wait=True, wait_timeout=10.0, poll_interval=0.0)
    assert sequence == []


async def test_compile_wait_returns_on_paused(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("COLUMNS", "400")
    mod = _reload()

    sequence = [
        {"state": "running", "last_compile_at": "t1"},
        {
            "state": "paused",
            "last_compile_at": "t2",
            "paused_reason": "ANTHROPIC_API_KEY missing",
        },
    ]

    async def _fake_compile(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"job_id": "j1", "status": "queued"}

    async def _fake_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return sequence.pop(0)

    monkeypatch.setattr(mod, "call_compile", _fake_compile)
    monkeypatch.setattr(mod, "call_compile_status", _fake_status)
    await mod.run_compile_cmd(None, wait=True, wait_timeout=10.0, poll_interval=0.0)
    combined = capsys.readouterr().out.replace("\n", " ")
    assert "Compile paused" in combined
    assert "ANTHROPIC_API_KEY missing" in combined


async def test_compile_wait_timeout_exits_5(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("COLUMNS", "400")
    mod = _reload()

    async def _fake_compile(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"job_id": "j1", "status": "queued"}

    async def _fake_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"state": "running", "last_compile_at": "tX"}

    monkeypatch.setattr(mod, "call_compile", _fake_compile)
    monkeypatch.setattr(mod, "call_compile_status", _fake_status)
    with pytest.raises(SystemExit) as excinfo:
        await mod.run_compile_cmd(
            None, wait=True, wait_timeout=0.01, poll_interval=0.0
        )
    assert excinfo.value.code == 5


async def test_compile_no_workspace_resolved_exits_2(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # config without default_workspace + clear env
    config_dir = temp_config_dir["config_dir"]
    (config_dir / "config.yaml").write_text(
        "server_url: http://localhost:8000\nllm:\n  provider: anthropic\n  model: claude-sonnet-4-6\n  api_key_env: ANTHROPIC_API_KEY\n  timeout_seconds: 120\n"
    )
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.delenv("KEENYSPACE_WORKSPACE", raising=False)
    monkeypatch.setenv("COLUMNS", "400")
    mod = _reload()
    with pytest.raises(SystemExit) as excinfo:
        await mod.run_compile_cmd(None, wait=False)
    assert excinfo.value.code == 2
