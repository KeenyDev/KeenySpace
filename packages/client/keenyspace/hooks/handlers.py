"""Five Claude Code hook handlers.

Each handler:
  1. Reads JSON envelope from stdin (Claude Code passes the hook payload there)
  2. Augments with kind/ts/workspace_slug
  3. Writes JSONL to the daemon socket (fire-and-forget, or request-response
     for session-start with source=compact)
  4. Returns — hook MUST exit 0 even on socket/parse failure.

All heavy imports stay deferred: the hook entry takes <1s wall-clock and
must not pull pydantic-ai / fastmcp / anthropic.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

from keenyspace.hooks.uds_client import fire_and_forget, request_response
from keenyspace.workspace_inference import resolve_workspace_slug


def _read_stdin_envelope() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("WARN: malformed Claude Code hook envelope", file=sys.stderr)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _augment(
    kind: str,
    claude_envelope: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    cwd = claude_envelope.get("cwd") or os.getcwd()
    # Slug inference must never crash a hook: a missing config.yaml or a
    # broken workspace-map.yaml leaves slug=None — daemon then sees a
    # workspace_slug=null envelope and handles that gracefully.
    try:
        slug, source = resolve_workspace_slug(cwd=cwd)
    except Exception:  # broad: hook MUST exit 0 even on config errors
        slug = None
        source = "unresolved"
    if source == "default":
        from keenyspace.hooks.dropped import increment

        increment("unmapped-workspace")
        # WHY stderr not structlog: hook <1s cold-boot budget forbids the
        # structlog import — matches uds_client.py / _read_stdin_envelope pattern.
        print(f"WARN: hook event dropped, no workspace mapping for cwd={cwd}", file=sys.stderr)
        return None
    env: dict[str, Any] = {
        "kind": kind,
        "ts": datetime.now(UTC).isoformat(),
        "workspace_slug": slug,
        "payload": claude_envelope,
    }
    if extra:
        env.update(extra)
    return env


async def handle_post_tool() -> None:
    env = _augment("post-tool", _read_stdin_envelope())
    if env is None:
        return
    await fire_and_forget(env)


async def handle_session_start() -> None:
    claude = _read_stdin_envelope()
    source = claude.get("source")
    env = _augment(
        "session-start",
        claude,
        extra={"source": source, "transcript_path": claude.get("transcript_path")},
    )
    if env is None:
        return
    if source == "compact":
        # Re-injection happens here, not on PostCompact (Claude Code spec).
        content = await request_response(env, counter_key="session-start.compact")
        if content:
            sys.stdout.write(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": content,
                        }
                    }
                )
            )
        # Graceful degradation: empty content -> exit 0 without stdout output.
        return
    await fire_and_forget(env)


async def handle_session_end() -> None:
    env = _augment("session-end", _read_stdin_envelope())
    if env is None:
        return
    await fire_and_forget(env)


async def handle_pre_compact() -> None:
    env = _augment("pre-compact", _read_stdin_envelope())
    if env is None:
        return
    await fire_and_forget(env)


async def handle_post_compact() -> None:
    # Post-compact stays a fire-and-forget audit event; Claude Code ignores
    # its stdout, so nothing is written to stdout here.
    env = _augment("post-compact", _read_stdin_envelope())
    if env is None:
        return
    await fire_and_forget(env)
