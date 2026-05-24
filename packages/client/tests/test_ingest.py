"""Tests for cli/ingest.py — CLI-13 invariant, budget, dir-concat, overflow."""

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


def _seed_config(config_dir: Path, *, default_workspace: str = "demo") -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        "\n".join(
            [
                "server_url: http://localhost:8000",
                f"default_workspace: {default_workspace}",
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

    import keenyspace.cli.ingest as ingest_mod
    import keenyspace.clients.llm as llm_mod
    import keenyspace.clients.mcp as mcp_mod

    importlib.reload(mcp_mod)
    importlib.reload(llm_mod)
    importlib.reload(ingest_mod)
    return ingest_mod


def _default_instructions(tool_whitelist: list[str] | None = None) -> Instructions:
    return Instructions(
        prompt="be helpful",
        tool_whitelist=tool_whitelist
        or ["search_workspace", "read_page", "append_log"],
        steps=["one"],
        model=None,
        budgets=Budgets(max_steps=5, max_tokens=10_000, max_seconds=30),
    )


async def test_ingest_calls_get_instructions_first(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    ingest_mod = _reload_modules()

    source = temp_config_dir["home"] / "notes.md"
    source.write_text("# hello\n")

    order: list[str] = []

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        order.append("get_instructions")
        return _default_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:
        order.append("run_server_driven_command")
        return "extracted=1"

    monkeypatch.setattr(ingest_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(
        ingest_mod, "run_server_driven_command", _fake_run
    )
    await ingest_mod.run_ingest(source)
    assert order == ["get_instructions", "run_server_driven_command"]


async def test_ingest_budget_enforced(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    ingest_mod = _reload_modules()

    source = temp_config_dir["home"] / "notes.md"
    source.write_text("# big file\n" * 100)

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        return _default_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:
        from keenyspace.agent.budgets import BudgetAbort

        raise BudgetAbort("usage_limit_exceeded")

    monkeypatch.setattr(ingest_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(ingest_mod, "run_server_driven_command", _fake_run)

    with pytest.raises(SystemExit) as excinfo:
        await ingest_mod.run_ingest(source)
    assert excinfo.value.code == 3


async def test_ingest_directory_concatenates_md_files(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    ingest_mod = _reload_modules()

    notes_dir = temp_config_dir["home"] / "notes"
    notes_dir.mkdir()
    (notes_dir / "a.md").write_text("alpha content")
    (notes_dir / "b.md").write_text("beta content")
    (notes_dir / "c.txt").write_text("should be ignored")

    captured: dict[str, Any] = {}

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        captured["context"] = kwargs.get("context")
        return _default_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:
        captured["user_prompt"] = kwargs.get("user_prompt")
        return "ok"

    monkeypatch.setattr(ingest_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(ingest_mod, "run_server_driven_command", _fake_run)
    await ingest_mod.run_ingest(notes_dir)

    body = captured["user_prompt"]
    assert "alpha content" in body
    assert "beta content" in body
    assert "should be ignored" not in body
    assert '<file path="a.md">' in body
    assert '<file path="b.md">' in body


async def test_ingest_no_workspace_resolved_exits_2(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No default_workspace in config + no slug-marker → unresolved.
    config_dir = temp_config_dir["config_dir"]
    (config_dir / "config.yaml").write_text(
        "server_url: http://localhost:8000\nllm:\n  provider: anthropic\n  model: claude-sonnet-4-6\n  api_key_env: ANTHROPIC_API_KEY\n  timeout_seconds: 120\n"
    )
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.delenv("KEENYSPACE_WORKSPACE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    ingest_mod = _reload_modules()

    source = temp_config_dir["home"] / "notes.md"
    source.write_text("hi")
    with pytest.raises(SystemExit) as excinfo:
        await ingest_mod.run_ingest(source)
    assert excinfo.value.code == 2


async def test_ingest_no_auth_exits_2(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    # no auth.json
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    ingest_mod = _reload_modules()

    source = temp_config_dir["home"] / "notes.md"
    source.write_text("hi")
    with pytest.raises(SystemExit) as excinfo:
        await ingest_mod.run_ingest(source)
    assert excinfo.value.code == 2


async def test_ingest_context_overflow_clean_error(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_config(temp_config_dir["config_dir"])
    _seed_auth(temp_config_dir["config_dir"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("COLUMNS", "400")
    ingest_mod = _reload_modules()

    source = temp_config_dir["home"] / "huge.md"
    source.write_text("payload")

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        return _default_instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "messages: prompt too long: 200000 input tokens > 180000 context window"
        )

    monkeypatch.setattr(ingest_mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(ingest_mod, "run_server_driven_command", _fake_run)
    with pytest.raises(SystemExit) as excinfo:
        await ingest_mod.run_ingest(source)
    assert excinfo.value.code == 2
    out = capsys.readouterr()
    # Strip rich's soft line wraps so the assertion is robust to TTY width.
    combined = (out.err + out.out).replace("\n", " ")
    assert "exceeds provider context budget" in combined
    assert "chunking deferred" in combined
