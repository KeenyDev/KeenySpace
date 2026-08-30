"""`keenyspace workspace ...` subcommands.

list / use / archive / from-cwd are thin httpx wrappers over the Phase 4
endpoints; pull defers to keenyspace.cli.pull.run_pull.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
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


@workspace_app.command("register")
def register_cmd(
    slug: str,
    path: str | None = typer.Argument(None),
    force: bool = typer.Option(False, "--force"),
    marker: bool = typer.Option(False, "--marker"),
) -> None:
    """Bind a directory to a workspace slug in workspace-map.yaml (or as a marker)."""

    asyncio.run(_run_register(slug, path, force=force, marker=marker))


@workspace_app.command("unregister")
def unregister_cmd(
    path: str | None = typer.Argument(None),
) -> None:
    """Remove the directory->workspace mapping for PATH (or git toplevel / cwd)."""

    _run_unregister(path)


@workspace_app.command("registrations")
def registrations_cmd() -> None:
    """Print a table of all registered path->slug entries."""

    _run_registrations()


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

    from keenyspace.clients.http import build_authed_http_client

    status = "all" if archived else "active"
    async with await build_authed_http_client() as client:
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

    from keenyspace.clients.http import build_authed_http_client
    from keenyspace.config import load_config_yaml
    from keenyspace.fs.atomic import write_atomic
    from keenyspace.paths import CONFIG_DIR, CONFIG_YAML

    async with await build_authed_http_client() as client:
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


def _resolve_target_path(path: str | None) -> str:
    if path is not None:
        return str(Path(path).resolve())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
        )
        if result.returncode == 0:
            return str(Path(result.stdout.strip()).resolve())
    except (FileNotFoundError, OSError):
        pass
    return str(Path(os.getcwd()).resolve())


async def _run_register(
    slug: str,
    path: str | None,
    *,
    force: bool,
    marker: bool,
) -> None:
    import json

    import yaml

    from keenyspace.clients.http import build_authed_http_client
    from keenyspace.fs.atomic import write_atomic
    from keenyspace.paths import CONFIG_DIR, WORKSPACE_MAP_YAML

    abs_path = _resolve_target_path(path)
    console = _console()

    try:
        import httpx

        async with await build_authed_http_client() as client:
            resp = await client.get(f"/v1/api/workspaces/{slug}")
        if resp.status_code == 404:
            console.print(f"[red]workspace not found:[/red] {slug}")
            sys.exit(2)
        resp.raise_for_status()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # WHY: server unreachable at register time is non-fatal; the map entry
        # is still useful for local routing and can be validated later.
        console.print("[yellow]warning: server unreachable, registering without validation[/yellow]")

    if marker:
        marker_dir = Path(abs_path) / ".keenyspace"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_path = marker_dir / "slug-marker.json"
        write_atomic(marker_path, json.dumps({"slug": slug}).encode())
        console.print(f"[green]Marker written to {marker_path}[/green]")
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    if WORKSPACE_MAP_YAML.is_file():
        loaded = yaml.safe_load(WORKSPACE_MAP_YAML.read_text()) or {}
        raw = loaded if isinstance(loaded, dict) else {}
    paths_map_raw = raw.get("paths") or {}
    paths_map: dict[str, str] = paths_map_raw if isinstance(paths_map_raw, dict) else {}

    if abs_path in paths_map:
        existing = paths_map[abs_path]
        if existing == slug:
            console.print(f"[dim]{abs_path} already registered as {slug}[/dim]")
            return
        if not force:
            console.print(f"[yellow]{abs_path} -> {existing}[/yellow]")
            console.print("[red]refusing without --force[/red]")
            sys.exit(1)

    paths_map[abs_path] = slug
    raw["paths"] = paths_map
    write_atomic(WORKSPACE_MAP_YAML, yaml.safe_dump(raw, sort_keys=False).encode())
    console.print(f"[green]Registered {abs_path} -> {slug}[/green]")


def _run_unregister(path: str | None) -> None:
    import yaml

    from keenyspace.fs.atomic import write_atomic
    from keenyspace.paths import WORKSPACE_MAP_YAML

    abs_path = _resolve_target_path(path)
    console = _console()

    if not WORKSPACE_MAP_YAML.is_file():
        console.print(f"[yellow]no registration for {abs_path}[/yellow]")
        sys.exit(2)

    raw: dict[str, Any] = {}
    loaded = yaml.safe_load(WORKSPACE_MAP_YAML.read_text()) or {}
    raw = loaded if isinstance(loaded, dict) else {}
    paths_map_raw = raw.get("paths") or {}
    paths_map: dict[str, str] = paths_map_raw if isinstance(paths_map_raw, dict) else {}

    if abs_path not in paths_map:
        console.print(f"[yellow]no registration for {abs_path}[/yellow]")
        sys.exit(2)

    del paths_map[abs_path]
    raw["paths"] = paths_map
    write_atomic(WORKSPACE_MAP_YAML, yaml.safe_dump(raw, sort_keys=False).encode())
    console.print(f"[green]Unregistered {abs_path}[/green]")


def _run_registrations() -> None:
    import yaml
    from rich.table import Table

    from keenyspace.paths import WORKSPACE_MAP_YAML

    console = _console()
    if not WORKSPACE_MAP_YAML.is_file():
        console.print("[dim]no registrations[/dim]")
        return

    raw: dict[str, Any] = {}
    loaded = yaml.safe_load(WORKSPACE_MAP_YAML.read_text()) or {}
    raw = loaded if isinstance(loaded, dict) else {}
    paths_map_raw = raw.get("paths") or {}
    paths_map: dict[str, str] = paths_map_raw if isinstance(paths_map_raw, dict) else {}

    if not paths_map:
        console.print("[dim]no registrations[/dim]")
        return

    table = Table(title="Registrations")
    table.add_column("path")
    table.add_column("slug")
    for p in sorted(paths_map):
        table.add_row(p, paths_map[p])
    console.print(table)


async def _run_archive(slug: str) -> None:
    from keenyspace.clients.http import build_authed_http_client

    async with await build_authed_http_client() as client:
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
