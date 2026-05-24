"""Typer entry point for the keenyspace CLI.

Pitfall #1: keep top-level imports minimal — only typer. Every command
body MUST defer heavy deps (pydantic-ai, httpx, fastmcp, anthropic, yaml,
rich) inside the function. The cold-boot test (test_cli_startup_time.py)
asserts `keenyspace --help` exits under 600ms.
"""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich", add_completion=False)
workspace_app = typer.Typer(name="workspace", help="Workspace operations")
hook_app = typer.Typer(name="hook", help="Internal: Claude Code hook entry points")
app.add_typer(workspace_app)
app.add_typer(hook_app, hidden=True)

# --- 05-05 daemon/hook/service registration ---
from keenyspace.cli.service import service_app  # noqa: E402
from keenyspace.daemon.cli import daemon_app  # noqa: E402

app.add_typer(daemon_app)
app.add_typer(service_app)


@hook_app.command("post-tool")
def hook_post_tool() -> None:
    """Claude Code PostToolUse event: forward to daemon as fire-and-forget JSONL."""
    import asyncio

    from keenyspace.hooks.handlers import handle_post_tool

    asyncio.run(handle_post_tool())


@hook_app.command("session-start")
def hook_session_start() -> None:
    """Claude Code SessionStart event; source=compact triggers context re-injection (F-09)."""
    import asyncio

    from keenyspace.hooks.handlers import handle_session_start

    asyncio.run(handle_session_start())


@hook_app.command("session-end")
def hook_session_end() -> None:
    """Claude Code SessionEnd event: fire-and-forget audit."""
    import asyncio

    from keenyspace.hooks.handlers import handle_session_end

    asyncio.run(handle_session_end())


@hook_app.command("pre-compact")
def hook_pre_compact() -> None:
    """Claude Code PreCompact event: fire-and-forget audit."""
    import asyncio

    from keenyspace.hooks.handlers import handle_pre_compact

    asyncio.run(handle_pre_compact())


@hook_app.command("post-compact")
def hook_post_compact() -> None:
    """Claude Code PostCompact event: fire-and-forget audit (no stdout injection per F-09)."""
    import asyncio

    from keenyspace.hooks.handlers import handle_post_compact

    asyncio.run(handle_post_compact())


# --- end 05-05 ---


@app.callback()
def _root_callback() -> None:
    from keenyspace.auth import _validate_auth_file_mode

    _validate_auth_file_mode()
    # Register `workspace` subcommands lazily so cold-boot of `--help` stays
    # under the 600ms target — the module body only adds @workspace_app.command
    # decorators and lightweight imports.
    import keenyspace.cli.workspace  # noqa: F401


@app.command(name="init")
def init_cmd() -> None:
    """Interactive first-time setup: server URL → write config.yaml → optional login."""
    import asyncio

    from keenyspace.cli.init_cmd import run_init

    asyncio.run(run_init())


@app.command(name="login")
def login_cmd(
    server_url: str | None = typer.Option(
        None, "--server-url", help="Override config.yaml server_url for this login."
    ),
) -> None:
    """Run Authentik device-code flow and persist token to auth.json (mode 0600)."""
    import asyncio

    from keenyspace.cli.login import run_login

    asyncio.run(run_login(server_url=server_url))


@app.command(name="logout")
def logout_cmd() -> None:
    """Clear server session via /v1/api/auth/logout and remove auth.json."""
    import asyncio

    from keenyspace.cli.login import run_logout

    asyncio.run(run_logout())


@app.command(name="status")
def status_cmd() -> None:
    """Print server URL, current user, default workspace, daemon socket reachability."""
    import asyncio

    from keenyspace.cli.status import run_status

    asyncio.run(run_status())


# --- 05-04 ingest/query/lint/compile commands ---


@app.command(name="ingest")
def ingest_cmd(
    path: str = typer.Argument(..., help="File or directory of .md to ingest"),
    workspace: str | None = typer.Option(
        None, "--workspace", help="Override resolved workspace slug"
    ),
) -> None:
    """Server-driven ingest: extract knowledge fragments from <path> into WAL."""
    import asyncio
    from pathlib import Path

    from keenyspace.cli.ingest import run_ingest

    asyncio.run(run_ingest(Path(path), workspace=workspace))


@app.command(name="query")
def query_cmd(
    question: str = typer.Argument(..., help="Question to ask the workspace agent"),
    workspace: str | None = typer.Option(
        None, "--workspace", help="Override resolved workspace slug"
    ),
) -> None:
    """Read-only Q&A over workspace knowledge."""
    import asyncio

    from keenyspace.cli.query import run_query

    asyncio.run(run_query(question, workspace=workspace))


@app.command(name="lint")
def lint_cmd(
    workspace: str | None = typer.Option(
        None, "--workspace", help="Override resolved workspace slug"
    ),
) -> None:
    """Wiki health audit (broken wikilinks, orphan pages, frontmatter schema)."""
    import asyncio

    from keenyspace.cli.lint import run_lint

    asyncio.run(run_lint(workspace=workspace))


@app.command(name="compile")
def compile_cmd(
    workspace: str | None = typer.Option(
        None, "--workspace", help="Override resolved workspace slug"
    ),
    wait: bool = typer.Option(
        False, "--wait", help="Block until compile finishes or 5min timeout"
    ),
) -> None:
    """Trigger server compile pass (fire-and-forget; --wait polls compile_status)."""
    import asyncio

    from keenyspace.cli.compile_cmd import run_compile_cmd

    asyncio.run(run_compile_cmd(workspace, wait=wait))


# --- end 05-04 ---


if __name__ == "__main__":
    app()
