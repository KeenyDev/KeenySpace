"""`keenyspace restore <archive>` — multipart upload to POST /v1/admin/restore.

Surfaces server-side 422 (version/schema/tarfile filter) and 409
(target_not_empty) as exit code 6 with a structured error display, prompting
the user toward --force. Happy path (200) prints the response payload and
exits 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from rich.console import Console

from keenyspace.cli.login import ensure_token
from keenyspace.config import get_client_settings

EXIT_CONFIG = 2
EXIT_REFUSED = 6  # restore refused (422 / 409) per CONTEXT Claude's Discretion


def _error_payload(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {"error": "unknown", "raw": response.text[:2000]}
    return payload if isinstance(payload, dict) else {"detail": payload}


async def run_restore(archive: Path, force: bool) -> None:
    console = Console()
    settings = get_client_settings()
    token = await ensure_token()
    if not token:
        console.print("[red]Not logged in.[/red] Run `keenyspace login` first.")
        sys.exit(EXIT_CONFIG)
    if not archive.exists():
        console.print(f"[red]Archive not found:[/red] {archive}")
        sys.exit(EXIT_CONFIG)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=settings.server_url,
        timeout=1800.0,
        headers=headers,
    ) as client:
        with archive.open("rb") as fp:
            response = await client.post(
                "/v1/admin/restore",
                params={"force": "true" if force else "false"},
                files={"file": (archive.name, fp, "application/gzip")},
            )
    if response.status_code in (422, 409):
        console.print("[red]Restore refused:[/red]")
        console.print(json.dumps(_error_payload(response), indent=2))
        if not force:
            console.print(
                "\nUse [yellow]--force[/yellow] to override "
                "(irreversible — wipes existing data)."
            )
        sys.exit(EXIT_REFUSED)
    if response.status_code >= 400:
        # The server puts the actual cause (e.g. psql stderr) in the body;
        # raise_for_status alone would drop it and leave only a status code.
        console.print(f"[red]Restore failed (HTTP {response.status_code}):[/red]")
        console.print(json.dumps(_error_payload(response), indent=2))
        sys.exit(1)
    console.print(f"[green]Restore complete:[/green] {response.json()}")
