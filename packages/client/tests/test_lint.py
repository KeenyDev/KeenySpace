"""Tests for cli/lint.py — CLI-13 + Pitfall #7 defence + read-only whitelist."""

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

    import keenyspace.cli.lint as lint_mod
    import keenyspace.clients.llm as llm_mod
    import keenyspace.clients.mcp as mcp_mod

    importlib.reload(mcp_mod)
    importlib.reload(llm_mod)
    importlib.reload(lint_mod)
    return lint_mod


def _readonly_instructions() -> Instructions:
    return Instructions(
        prompt="audit wiki health",
        tool_whitelist=["search_workspace", "read_page", "list_pages"],
        steps=["check"],
        model=None,
        budgets=Budgets(max_steps=5, max_tokens=10_000, max_seconds=30),
    )


async def test_lint_calls_get_instructions_first(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    lint_mod = _reload_modules()

    order: list[str] = []

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        order.append("get_instructions")
        return _readonly_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:
        order.append("run_server_driven_command")
        return "no issues"

    monkeypatch.setattr(lint_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(lint_mod, "run_server_driven_command", _fake_run)
    await lint_mod.run_lint()
    assert order == ["get_instructions", "run_server_driven_command"]


async def test_lint_append_log_refused(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    lint_mod = _reload_modules()

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        return Instructions(
            prompt="oops",
            tool_whitelist=["read_page", "append_log"],
            steps=["one"],
            model=None,
            budgets=Budgets(max_steps=5, max_tokens=10_000, max_seconds=30),
        )

    async def _fake_run(*args: Any, **kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("should not be called")

    monkeypatch.setattr(lint_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(lint_mod, "run_server_driven_command", _fake_run)

    with pytest.raises(SystemExit) as excinfo:
        await lint_mod.run_lint()
    assert excinfo.value.code == 2
    out = capsys.readouterr()
    combined = out.err + out.out
    assert "Defence-in-depth" in combined


async def test_lint_uses_read_only_whitelist(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    lint_mod = _reload_modules()

    captured: dict[str, Any] = {}

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        return _readonly_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:
        captured["instructions"] = kwargs.get("instructions")
        return "audit complete"

    monkeypatch.setattr(lint_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(lint_mod, "run_server_driven_command", _fake_run)
    await lint_mod.run_lint()
    instr: Instructions = captured["instructions"]
    assert "append_log" not in instr.tool_whitelist
    assert set(instr.tool_whitelist) == {"search_workspace", "read_page", "list_pages"}
