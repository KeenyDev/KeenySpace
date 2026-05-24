"""`keenyspace workspace ...` subcommands.

list / use / archive / from-cwd are thin httpx wrappers over the Phase 4
endpoints; pull defers to keenyspace.cli.pull.run_pull.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import typer

from keenyspace.__main__ import workspace_app


@workspace_app.command("list")
def list_cmd(
    archived: bool = typer.Option(
        False, "--archived", help="Include archived workspaces."
    ),
) -> None:
    """List workspaces (active by default; pass --archived for all)."""

    asyncio.run(_run_list(archived))


@workspace_app.command("use")
def use_cmd(slug: str) -> None:
    """Set <slug> as the default workspace in config.yaml."""

    asyncio.run(_run_use(slug))


@workspace_app.command("archive")
def archive_cmd(slug: str) -> None:
    """Archive a workspace via POST /v1/api/workspaces/<slug>/archive."""

    asyncio.run(_run_archive(slug))


@workspace_app.command("from-cwd")
def from_cwd_cmd() -> None:
    """Print the workspace slug inferred from cwd plus its source."""

    from keenyspace.workspace_inference import resolve_workspace_slug

    slug, source = resolve_workspace_slug()
    console = _console()
    if slug is None:
        console.print("[yellow]unresolved[/yellow]")
        console.print(f"source: {source}")
        raise typer.Exit(code=2)
    console.print(slug)
    console.print(f"source: {source}")


@workspace_app.command("pull")
def pull_cmd(
    slug: str,
    force: bool = typer.Option(
        False, "--force", help="Stash dirty files to conflicts/ then apply server canon."
    ),
) -> None:
    """Sync vault from server. Refuses dirty state without --force (exit 4)."""

    from keenyspace.cli.pull import run_pull

    asyncio.run(run_pull(slug, force=force))


def _console() -> Any:
    from rich.console import Console

    return Console()


async def _run_list(archived: bool) -> None:
    from rich.table import Table

    from keenyspace.clients.http import build_http_client

    status = "all" if archived else "active"
    async with build_http_client() as client:
        resp = await client.get(
            "/v1/api/workspaces/", params={"status": status}
        )
        resp.raise_for_status()
        payload = resp.json()
    workspaces = payload.get("workspaces") or []
    table = Table(title="Workspaces")
    table.add_column("slug")
    table.add_column("blueprint")
    table.add_column("status")
    table.add_column("last_compile_at")
    for ws in workspaces:
        table.add_row(
            str(ws.get("slug", "")),
            str(ws.get("blueprint_pin", "")),
            str(ws.get("status", "")),
            str(ws.get("last_compile_at") or ""),
        )
    _console().print(table)


async def _run_use(slug: str) -> None:
    import yaml

    from keenyspace.clients.http import build_http_client
    from keenyspace.config import load_config_yaml
    from keenyspace.fs.atomic import write_atomic
    from keenyspace.paths import CONFIG_DIR, CONFIG_YAML

    async with build_http_client() as client:
        resp = await client.get(f"/v1/api/workspaces/{slug}")
    console = _console()
    if resp.status_code == 404:
        console.print(f"[red]workspace not found:[/red] {slug}")
        sys.exit(2)
    resp.raise_for_status()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current = load_config_yaml(CONFIG_YAML)
    current["default_workspace"] = slug
    payload = yaml.safe_dump(current, sort_keys=False).encode()
    write_atomic(CONFIG_YAML, payload)
    console.print(f"[green]Default workspace set to {slug}[/green]")


async def _run_archive(slug: str) -> None:
    from keenyspace.clients.http import build_http_client

    async with build_http_client() as client:
        resp = await client.post(f"/v1/api/workspaces/{slug}/archive")
    console = _console()
    if resp.status_code == 200:
        console.print(f"[green]Workspace {slug} archived[/green]")
        return
    if resp.status_code == 404:
        console.print(f"[red]workspace not found:[/red] {slug}")
        sys.exit(2)
    if resp.status_code == 409:
        console.print(f"[yellow]Already archived:[/yellow] {slug}")
        return
    console.print(f"[red]archive failed ({resp.status_code}):[/red] {resp.text}")
    sys.exit(1)
