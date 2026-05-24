"""CLI-13 invariant test across all three server-driven commands.

Every non-trivial command (ingest, query, lint) MUST call `get_instructions`
before any LLM work — the invariant that lets Phase 5's server-driven
philosophy hold under regression.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from keenyspace_shared.mcp_contracts import Budgets, Instructions


def _seed_auth(home: Path) -> None:
    auth = home / ".config" / "keenyspace" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"api_key": "ks_live_test"}))
    os.chmod(auth, stat.S_IRUSR | stat.S_IWUSR)


def _seed_config(home: Path) -> None:
    config = home / ".config" / "keenyspace" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
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


def _instructions(tool_whitelist: list[str] | None = None) -> Instructions:
    return Instructions(
        prompt="be helpful",
        tool_whitelist=tool_whitelist
        or ["search_workspace", "read_page", "list_pages"],
        steps=["one"],
        model=None,
        budgets=Budgets(max_steps=5, max_tokens=10_000, max_seconds=30),
    )


def _reload_chain(target_module: str) -> Any:
    for name in (
        "keenyspace.paths",
        "keenyspace.config",
        "keenyspace.auth",
        "keenyspace.workspace_inference",
        "keenyspace.clients.mcp",
        "keenyspace.clients.llm",
        target_module,
    ):
        importlib.reload(importlib.import_module(name))
    import keenyspace.config as cfg_mod

    cfg_mod.get_client_settings.cache_clear()
    return importlib.import_module(target_module)


@pytest.mark.parametrize(
    "module_name,run_fn,kwargs",
    [
        ("keenyspace.cli.ingest", "run_ingest", {}),
        ("keenyspace.cli.query", "run_query", {"question": "what?"}),
        ("keenyspace.cli.lint", "run_lint", {}),
    ],
)
async def test_every_command_calls_get_instructions_first(
    temp_config_dir: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    run_fn: str,
    kwargs: dict[str, Any],
) -> None:
    _seed_config(temp_config_dir["home"])
    _seed_auth(temp_config_dir["home"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    mod = _reload_chain(module_name)

    order: list[str] = []

    async def _fake_get_instructions(*args: Any, **kwargs: Any) -> Instructions:
        order.append("get_instructions")
        return _instructions()

    async def _fake_run(*args: Any, **kwargs: Any) -> str:
        order.append("run_server_driven_command")
        return "ok"

    monkeypatch.setattr(mod, "get_instructions", _fake_get_instructions)
    monkeypatch.setattr(mod, "run_server_driven_command", _fake_run)

    if run_fn == "run_ingest":
        source = temp_config_dir["home"] / "notes.md"
        source.write_text("# hi\n")
        await mod.run_ingest(source, **kwargs)
    else:
        await getattr(mod, run_fn)(**kwargs)
    assert order[0] == "get_instructions"
    assert "run_server_driven_command" in order
    assert order.index("get_instructions") < order.index(
        "run_server_driven_command"
    )
