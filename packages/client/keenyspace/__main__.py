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
daemon_app = typer.Typer(name="daemon", help="Background daemon controls")
hook_app = typer.Typer(name="hook", help="Internal: Claude Code hook entry points")
service_app = typer.Typer(name="service", help="OS service registration")
app.add_typer(workspace_app)
app.add_typer(daemon_app)
app.add_typer(hook_app, hidden=True)
app.add_typer(service_app)


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


if __name__ == "__main__":
    app()
