"""Phase 5 D-02/D-03: server-driven ingest command.

Reads source file (or recursively concatenates .md files from a directory)
into a single LLM prompt, fetches Instructions from server first (CLI-13),
then runs a pydantic-ai Agent that has access to `append_log` via MCP.

D-03 v1 invariant: whole file in one prompt; chunking deferred to v1.5.
Provider context-overflow surfaces as a clean error (exit 2), NOT a stack
trace.

Exit codes:
  0 — success
  2 — config / auth / context-overflow
  3 — budget abort (UsageLimitExceeded / timeout / loop)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import structlog

from keenyspace.agent.budgets import BudgetAbort, log_aborted
from keenyspace.auth import read_auth
from keenyspace.clients.llm import run_server_driven_command
from keenyspace.clients.mcp import get_instructions
from keenyspace.config import get_client_settings
from keenyspace.workspace_inference import resolve_workspace_slug

log = structlog.get_logger(__name__)

EXIT_BUDGET_ABORT = 3
EXIT_CONFIG = 2


def _read_source(path: Path) -> str:
    if path.is_dir():
        chunks: list[str] = []
        for f in sorted(path.rglob("*.md")):
            rel = f.relative_to(path).as_posix()
            chunks.append(
                f'<file path="{rel}">\n{f.read_text(encoding="utf-8")}\n</file>'
            )
        return "\n\n".join(chunks)
    return path.read_text(encoding="utf-8")


def _is_context_overflow(exc: BaseException) -> bool:
    cls = exc.__class__.__name__
    if cls in {"BadRequestError", "OverloadedError"}:
        return True
    msg = str(exc).lower()
    return "context" in msg and ("token" in msg or "length" in msg)


async def run_ingest(path: Path, workspace: str | None = None) -> None:
    from rich.console import Console

    console = Console()
    err = Console(stderr=True)
    settings = get_client_settings()
    slug, _source = resolve_workspace_slug(explicit=workspace)
    if slug is None:
        err.print(
            "[red]No workspace resolved. Use --workspace or "
            "`keenyspace workspace use <slug>`.[/red]"
        )
        sys.exit(EXIT_CONFIG)
    if not path.exists():
        err.print(f"[red]Path not found: {path}[/red]")
        sys.exit(EXIT_CONFIG)
    auth = read_auth()
    api_key = auth.get("api_key") or auth.get("access_token")
    if not api_key:
        err.print("[red]Not logged in. Run `keenyspace login`.[/red]")
        sys.exit(EXIT_CONFIG)
    if not os.environ.get(settings.llm.api_key_env):
        err.print(
            f"[red]LLM API key env var {settings.llm.api_key_env} is not set.[/red]"
        )
        sys.exit(EXIT_CONFIG)
    source_content = _read_source(path)
    try:
        instructions = await get_instructions(
            settings.server_url,
            api_key,
            workspace=slug,
            command="ingest",
            context={"source_path": str(path), "source_content": source_content},
        )
        result = await run_server_driven_command(
            server_url=settings.server_url,
            api_key=api_key,
            instructions=instructions,
            user_prompt=source_content,
            llm_model=f"{settings.llm.provider}:{settings.llm.model}",
        )
        console.print(f"[green]Ingest complete[/green]: {result}")
    except BudgetAbort as ba:
        log_aborted(ba.reason, command="ingest", workspace=slug)
        err.print(
            f"[yellow]Ingest aborted ({ba.reason}). "
            "Partial appends may remain in WAL.[/yellow]"
        )
        sys.exit(EXIT_BUDGET_ABORT)
    except Exception as exc:
        if _is_context_overflow(exc):
            err.print(
                f"[red]File '{path}' exceeds provider context budget. "
                "Split manually or switch to a provider with larger context "
                "(chunking deferred to v1.5).[/red]"
            )
            sys.exit(EXIT_CONFIG)
        raise
