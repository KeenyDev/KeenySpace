"""Phase 5 + Phase 2 D-12/D-13: thin client over MCP `compile` + `compile_status`.

Default is fire-and-forget — the server queues the compile job and returns
immediately. `--wait` polls compile_status every 2s (default) until the
state becomes idle or paused, with a 5min hard timeout (exit 5).
"""

from __future__ import annotations

import asyncio
import sys

from keenyspace.auth import read_auth
from keenyspace.clients.mcp import call_compile, call_compile_status
from keenyspace.config import get_client_settings
from keenyspace.workspace_inference import resolve_workspace_slug

EXIT_CONFIG = 2
EXIT_COMPILE_TIMEOUT = 5


async def run_compile_cmd(
    workspace: str | None,
    *,
    wait: bool,
    wait_timeout: float = 300.0,
    poll_interval: float = 2.0,
) -> None:
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
    trigger = await call_compile(settings.server_url, api_key, workspace=slug)
    job_id = trigger.get("job_id", "?")
    status = trigger.get("status", "?")
    console.print(
        f"Compile triggered: workspace={slug} job_id={job_id} status={status}"
    )
    if not wait:
        return
    loop = asyncio.get_event_loop()
    deadline = loop.time() + wait_timeout
    while True:
        payload = await call_compile_status(
            settings.server_url, api_key, workspace=slug
        )
        state = payload.get("state", "?")
        last_at = payload.get("last_compile_at", "?")
        console.print(f"  state={state} last_compile_at={last_at}")
        if state in ("idle", "paused"):
            if state == "paused":
                reason = payload.get("paused_reason", "(unknown)")
                console.print(f"[yellow]Compile paused: {reason}[/yellow]")
            return
        if loop.time() > deadline:
            err.print(
                f"[red]Compile did not finish within {wait_timeout}s "
                f"(still {state}).[/red]"
            )
            sys.exit(EXIT_COMPILE_TIMEOUT)
        await asyncio.sleep(poll_interval)
