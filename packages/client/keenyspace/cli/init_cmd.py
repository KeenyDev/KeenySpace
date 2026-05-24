"""`keenyspace init` — first-time wizard."""

from __future__ import annotations

import typer
import yaml
from rich.console import Console

from keenyspace.fs.atomic import write_atomic
from keenyspace.paths import CONFIG_DIR, CONFIG_YAML


async def run_init() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    console = Console()
    console.print("[bold]KeenySpace CLI setup[/bold]")
    server_url_raw = typer.prompt("Server URL (e.g. https://keenyspace.example.com)")
    server_url = server_url_raw.rstrip("/")
    default_workspace = typer.prompt(
        "Default workspace slug (leave blank to skip)",
        default="",
        show_default=False,
    )

    payload: dict[str, str] = {"server_url": server_url}
    if default_workspace:
        payload["default_workspace"] = default_workspace

    write_atomic(CONFIG_YAML, yaml.safe_dump(payload, sort_keys=False).encode())
    console.print(f"[green]Wrote[/green] {CONFIG_YAML}")

    if typer.confirm("Login now?", default=True):
        from keenyspace.cli.login import run_login

        await run_login(server_url=None)
