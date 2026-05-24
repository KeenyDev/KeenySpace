"""`keenyspace workspace pull <slug>` — dirty-aware pull (D-10..D-13).

Workflow:
1. GET /v1/api/workspaces/<slug>/manifest -> server file map.
2. Walk local vault, compute sha256 manifest (scope = .md + raw/).
3. Diff. If dirty (modified | added | removed) and not --force: print summary, exit 4.
4. If --force: stash dirty bytes under conflicts/<iso>/, print unified diff.
5. Download every server file via /pages-raw/, atomic-write into vault.
6. Delete in-scope local files that vanished from server canon.
7. Write slug-marker.json (D-13 option b) + local-state.json (atomic 0o600).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXIT_DIRTY = 4


async def run_pull(
    slug: str,
    *,
    force: bool = False,
    target: Path | None = None,
) -> None:
    from rich.console import Console
    from rich.table import Table

    from keenyspace.clients.http import build_http_client
    from keenyspace.fs.atomic import write_atomic, write_atomic_secret
    from keenyspace.paths import DEFAULT_PULL_ROOT, STATE_DIR
    from keenyspace.pull.manifest import diff_manifests, hash_local_tree
    from keenyspace.pull.stash import render_diff, stash_dirty

    console = Console()
    target_path = target or (DEFAULT_PULL_ROOT / slug)
    state_dir = STATE_DIR / slug
    state_dir.mkdir(parents=True, exist_ok=True)
    local_state_path = state_dir / "local-state.json"

    async with build_http_client() as client:
        resp = await client.get(f"/v1/api/workspaces/{slug}/manifest")
        resp.raise_for_status()
        server_doc: dict[str, Any] = resp.json()
        server_files: dict[str, str] = dict(server_doc.get("files") or {})

        # D-11: a target dir that does not exist yet means "first pull" — no
        # files can be modified/added/removed relative to nothing, and the
        # server's manifest must NOT register every file as `removed`.
        first_pull = not target_path.exists()
        local_files = hash_local_tree(target_path)
        diff = diff_manifests(local_files, server_files)
        if first_pull:
            diff.modified.clear()
            diff.added.clear()
            diff.removed.clear()

        if diff.is_dirty and not force:
            table = Table(title=f"Dirty files for {slug}")
            table.add_column("Status")
            table.add_column("Path")
            for rel in diff.modified:
                table.add_row("modified", rel)
            for rel in diff.added:
                table.add_row("added", rel)
            for rel in diff.removed:
                table.add_row("removed", rel)
            console.print(table)
            console.print(
                "[red]Refusing to overwrite dirty state. Use --force to stash + apply server canon.[/red]"
            )
            sys.exit(EXIT_DIRTY)

        stash_root: Path | None = None
        preloaded_server: dict[str, bytes] = {}
        if diff.is_dirty:
            iso = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
            stash_root = state_dir / "conflicts" / iso
            stash_root.mkdir(parents=True, exist_ok=True)
            stash_dirty(diff, target_path, stash_root)
            for rel in diff.modified:
                preloaded_server[rel] = await _fetch_page_bytes(client, slug, rel)
            render_diff(diff, target_path, lambda rel: preloaded_server[rel])

        target_path.mkdir(parents=True, exist_ok=True)
        for rel, _server_hash in server_files.items():
            if rel in preloaded_server:
                payload_bytes = preloaded_server[rel]
            else:
                payload_bytes = await _fetch_page_bytes(client, slug, rel)
            dest = target_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(dest, payload_bytes)

        for rel in set(local_files) - set(server_files):
            (target_path / rel).unlink(missing_ok=True)

        marker = target_path / ".keenyspace" / "slug-marker.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_secret(
            marker, json.dumps({"slug": slug}, indent=2).encode()
        )

        new_manifest = {
            "version": 1,
            "workspace_slug": slug,
            "server_canon_at": server_doc.get("server_canon_at"),
            "last_pull_ts": datetime.now(UTC).isoformat(),
            "files": server_files,
        }
        write_atomic_secret(
            local_state_path, json.dumps(new_manifest, indent=2).encode()
        )

    console.print(
        f"[green]Pulled {len(server_files)} files to {target_path}[/green]"
    )
    if stash_root is not None:
        console.print(f"[yellow]Dirty files stashed to {stash_root}[/yellow]")


async def _fetch_page_bytes(client: Any, slug: str, rel: str) -> bytes:
    resp = await client.get(f"/v1/api/workspaces/{slug}/pages-raw/{rel}")
    resp.raise_for_status()
    content: bytes = resp.content
    return content
