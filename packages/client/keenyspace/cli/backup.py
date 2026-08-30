"""`keenyspace backup` — thin streamer over POST /v1/admin/backup.

httpx.AsyncClient.stream() drains the gzipped tarball into the chosen output
path, rendering a rich progress bar (DownloadColumn + TransferSpeedColumn).
Default output filename mirrors the server's Content-Disposition: ISO-stamped
keenyspace-backup-<iso>.tar.gz under cwd.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from keenyspace.cli.login import ensure_token
from keenyspace.config import get_client_settings

EXIT_CONFIG = 2


async def run_backup(output: Path | None) -> None:
    console = Console()
    settings = get_client_settings()
    token = await ensure_token()
    if not token:
        console.print("[red]Not logged in.[/red] Run `keenyspace login` first.")
        sys.exit(EXIT_CONFIG)
    if output is None:
        iso = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        output = Path.cwd() / f"keenyspace-backup-{iso}.tar.gz"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=settings.server_url,
        timeout=600.0,
        headers=headers,
    ) as client, client.stream("POST", "/v1/admin/backup") as response:
        response.raise_for_status()
        cl = response.headers.get("content-length")
        total = int(cl) if cl else None
        with Progress(
            TextColumn("[cyan]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task("Streaming backup", total=total)
            with output.open("wb") as fp:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    fp.write(chunk)
                    progress.update(task, advance=len(chunk))
    console.print(f"[green]Backup written to {output}[/green]")
