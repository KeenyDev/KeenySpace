"""Phase 5 D-04 + Pitfall #7 mitigation: read-only Q&A command.

Server returns Instructions with tool_whitelist limited to read_page /
search_workspace / list_pages. Client refuses to run if the server-supplied
whitelist contains `append_log` — defence-in-depth against server bug or
blueprint edit that accidentally re-includes the write tool.
"""

from __future__ import annotations

import os
import sys

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


async def run_query(question: str, workspace: str | None = None) -> None:
    from rich.console import Console

    console = Console()
    err = Console(stderr=True)
    settings = get_client_settings()
    slug, _source = resolve_workspace_slug(explicit=workspace)
    if slug is None:
        err.print("[red]No workspace resolved.[/red]")
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
    instructions = await get_instructions(
        settings.server_url,
        api_key,
        workspace=slug,
        command="query",
        context={"question": question},
    )
    # WHY: T-05.04-02 defence-in-depth. Server may regress and accidentally
    # include the write tool in a read-only command whitelist; the client
    # refuses to be the failure point.
    if "append_log" in instructions.tool_whitelist:
        err.print(
            "[red]Defence-in-depth: server returned a write tool in the query "
            "whitelist. Refusing to run.[/red]"
        )
        sys.exit(EXIT_CONFIG)
    try:
        result = await run_server_driven_command(
            server_url=settings.server_url,
            api_key=api_key,
            instructions=instructions,
            user_prompt=question,
            llm_model=f"{settings.llm.provider}:{settings.llm.model}",
        )
        console.print(result)
    except BudgetAbort as ba:
        log_aborted(ba.reason, command="query", workspace=slug)
        err.print(f"[yellow]Query aborted ({ba.reason}).[/yellow]")
        sys.exit(EXIT_BUDGET_ABORT)
