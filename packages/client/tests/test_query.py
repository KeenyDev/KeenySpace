"""Tests for cli/query.py — CLI-13 invariant + Pitfall #7 defence-in-depth."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from keenyspace_shared.mcp_contracts import Budgets, Instructions


def _seed_auth(config_dir: Path) -> None:
    auth_path = config_dir / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(json.dumps({"api_key": "ks_live_test"}))
    os.chmod(auth_path, stat.S_IRUSR | stat.S_IWUSR)


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


def _reload_modules() -> Any:
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

    import keenyspace.cli.query as query_mod
    import keenyspace.clients.llm as llm_mod
    import keenyspace.clients.mcp as mcp_mod

    importlib.reload(mcp_mod)
    importlib.reload(llm_mod)
    importlib.reload(query_mod)
    return query_mod


def _read_only_instructions() -> Instructions:
    return Instructions(
        prompt="answer the question",
        tool_whitelist=["read_page", "search_workspace", "list_pages"],
        steps=["search", "synthesise"],
        model=None,
        budgets=Budgets(max_steps=5, max_tokens=10_000, max_seconds=30),
    )


def _instructions_with_append_log() -> Instructions:
    return Instructions(
        prompt="oops",
        tool_whitelist=["read_page", "append_log"],
        steps=["search"],
        model=None,
        budgets=Budgets(max_steps=5, max_tokens=10_000, max_seconds=30),
    )


async def test_query_calls_get_instructions_first(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    query_mod = _reload_modules()

    order: list[str] = []

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        order.append("get_instructions")
        return _read_only_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:
        order.append("run_server_driven_command")
        return "an answer"

    monkeypatch.setattr(query_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(query_mod, "run_server_driven_command", _fake_run)
    await query_mod.run_query("what is foo?")
    assert order == ["get_instructions", "run_server_driven_command"]


async def test_query_append_log_in_whitelist_refused(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    query_mod = _reload_modules()

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        return _instructions_with_append_log()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("run_server_driven_command should not be called")

    monkeypatch.setattr(query_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(query_mod, "run_server_driven_command", _fake_run)

    with pytest.raises(SystemExit) as excinfo:
        await query_mod.run_query("what is foo?")
    assert excinfo.value.code == 2
    out = capsys.readouterr()
    combined = out.err + out.out
    assert "Defence-in-depth" in combined


async def test_query_budget_abort_exits_3(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    query_mod = _reload_modules()

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        return _read_only_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> None:
        from keenyspace.agent.budgets import BudgetAbort

        raise BudgetAbort("timeout_exceeded")

    monkeypatch.setattr(query_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(query_mod, "run_server_driven_command", _fake_run)

    with pytest.raises(SystemExit) as excinfo:
        await query_mod.run_query("what is foo?")
    assert excinfo.value.code == 3


async def test_query_renders_result(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    query_mod = _reload_modules()

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        return _read_only_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:
        return "Foo refers to the concept of bar"

    monkeypatch.setattr(query_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(query_mod, "run_server_driven_command", _fake_run)
    await query_mod.run_query("what is foo?")
    out = capsys.readouterr()
    assert "Foo refers" in out.out
