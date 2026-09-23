from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any, TypeVar


def build_auth_json(
    state_dir: Path,
    *,
    api_key: str = "ks_live_test",
    mode: int = 0o600,
) -> Path:
    """Write a minimal auth.json under state_dir/auth.json (mode 0600 by default).

    Login tests use this to seed credentials without going through the device-code
    flow.
    """

    auth_path = state_dir / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"api_key": api_key}
    auth_path.write_text(json.dumps(payload))
    os.chmod(auth_path, mode & (stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO))
    return auth_path


def build_config_yaml(
    config_dir: Path,
    *,
    server_url: str = "http://localhost:8000",
    default_workspace: str | None = None,
) -> Path:
    """Write a minimal client config.yaml under config_dir/config.yaml."""

    config_path = config_dir / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"server_url: {server_url}"]
    if default_workspace is not None:
        lines.append(f"default_workspace: {default_workspace}")
    lines.extend(
        [
            "llm:",
            "  provider: anthropic",
            "  model: claude-sonnet-4-6",
            "  api_key_env: ANTHROPIC_API_KEY",
            "  timeout_seconds: 120",
        ]
    )
    config_path.write_text("\n".join(lines) + "\n")
    return config_path


def make_envelope(
    kind: str,
    *,
    source: str | None = None,
    workspace_slug: str | None = "demo",
    **extra: Any,
) -> dict[str, Any]:
    """Construct a JSONL hook envelope shaped like Phase 5 D-06 IPC.

    ``kind`` is the hook event (post-tool, session-start, ...); ``source`` is
    only present on session-start (compact|startup|resume|clear).
    """

    env: dict[str, Any] = {"kind": kind}
    if source is not None:
        env["source"] = source
    if workspace_slug is not None:
        env["workspace_slug"] = workspace_slug
    env.update(extra)
    return env


T = TypeVar("T")


async def expect_within_seconds(
    coro: Any, seconds: float
) -> Any:
    """Wrap asyncio.wait_for; surfaces latency-budget violations as TimeoutError."""

    return await asyncio.wait_for(coro, timeout=seconds)
