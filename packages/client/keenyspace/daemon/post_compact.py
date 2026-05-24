"""Daemon-side post-compact context assembly (Phase 5 D-09).

Flow:
  1. Read transcript_path off the loop via daemon/transcript.read_transcript_excerpt.
  2. MCP get_instructions(workspace, "post-compact", {transcript_excerpt}) FIRST.
  3. Defence-in-depth: refuse if tool_whitelist contains append_log (T-05.06-07).
  4. clients.llm.run_server_driven_command with output_type=PostCompactInjection.
  5. Return {ok, content, error} per F-09 response shape.

Logged metadata is intentionally narrow (workspace, page count, text length).
Transcript content and assembled text are NEVER logged (T-05.06-05).
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from keenyspace_shared.mcp_contracts import (  # type: ignore[import-untyped]
    PostCompactInjection,
)

from keenyspace.agent.budgets import BudgetAbort
from keenyspace.auth import read_auth
from keenyspace.clients.llm import run_server_driven_command
from keenyspace.clients.mcp import get_instructions
from keenyspace.config import get_client_settings
from keenyspace.daemon.transcript import read_transcript_excerpt

log = structlog.get_logger(__name__)


_TEST_HATCH_ENV = "KEENYSPACE_TEST_AGENT_RESPONSE"


async def assemble_context(envelope: dict[str, Any]) -> dict[str, Any]:
    workspace_slug = envelope.get("workspace_slug")
    transcript_path = envelope.get("transcript_path") or (
        envelope.get("payload", {}).get("transcript_path")
        if isinstance(envelope.get("payload"), dict)
        else None
    )
    if not workspace_slug:
        log.warning("post_compact.no_workspace_slug")
        return {"ok": False, "content": None, "error": "no_workspace_slug"}
    if not transcript_path:
        log.warning("post_compact.no_transcript_path")
        return {"ok": False, "content": None, "error": "no_transcript_path"}

    excerpt = await read_transcript_excerpt(transcript_path)
    if excerpt is None:
        log.warning(
            "post_compact.transcript_unavailable", workspace=workspace_slug
        )
        return {
            "ok": False,
            "content": None,
            "error": "transcript_unavailable",
        }

    test_response = os.environ.get(_TEST_HATCH_ENV)
    if test_response is not None:
        log.info(
            "post_compact.test_hatch_active",
            workspace=workspace_slug,
            text_chars=len(test_response),
        )
        return {"ok": True, "content": test_response, "error": None}

    auth = read_auth()
    api_key = auth.get("api_key") or auth.get("access_token")
    if not api_key:
        log.warning("post_compact.no_auth", workspace=workspace_slug)
        return {"ok": False, "content": None, "error": "no_auth"}

    settings = get_client_settings()
    try:
        instructions = await get_instructions(
            settings.server_url,
            api_key,
            workspace=workspace_slug,
            command="post-compact",
            context={"transcript_excerpt": excerpt},
        )
        if "append_log" in instructions.tool_whitelist:
            log.warning(
                "post_compact.append_log_in_whitelist_refused",
                workspace=workspace_slug,
            )
            return {
                "ok": False,
                "content": None,
                "error": "append_log_in_whitelist",
            }
        result = await run_server_driven_command(
            server_url=settings.server_url,
            api_key=api_key,
            instructions=instructions,
            user_prompt=excerpt,
            llm_model=f"{settings.llm.provider}:{settings.llm.model}",
            output_type=PostCompactInjection,
        )
        if not isinstance(result, PostCompactInjection):
            log.warning(
                "post_compact.unexpected_output_type",
                workspace=workspace_slug,
                type=type(result).__name__,
            )
            return {
                "ok": False,
                "content": None,
                "error": "unexpected_output_type",
            }
        log.info(
            "post_compact.assembled",
            workspace=workspace_slug,
            selected_pages=len(result.selected_pages),
            text_chars=len(result.assembled_text),
        )
        return {"ok": True, "content": result.assembled_text, "error": None}
    except BudgetAbort as ba:
        log.warning(
            "post_compact.budget_abort",
            reason=ba.reason,
            workspace=workspace_slug,
        )
        return {
            "ok": False,
            "content": None,
            "error": f"budget_abort:{ba.reason}",
        }
    except Exception as exc:  # broad: daemon must survive any LLM/MCP error
        log.error(
            "post_compact.unexpected_error",
            err=str(exc),
            workspace=workspace_slug,
        )
        return {"ok": False, "content": None, "error": "unexpected_error"}
