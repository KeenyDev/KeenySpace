"""`keenyspace workspace pull <slug>` — dirty-aware pull.

Workflow:
1. GET /v1/api/workspaces/<slug>/manifest -> server file map. Any key that would
   resolve outside the vault aborts the pull (exit 7) before anything is written.
2. Walk local vault, compute sha256 manifest (scope = .md + raw/).
3. Diff. If dirty (modified | added | removed) and not --force: print summary, exit 4.
4. If --force: stash dirty bytes under conflicts/<iso>/, print unified diff.
5. Download every server file whose local hash differs via /pages-raw/ (at most
   FETCH_CONCURRENCY in flight), atomic-write into vault. The first failure
   cancels the remaining fetches and aborts before local-state.json is written.
6. Delete in-scope local files that vanished from server canon.
7. Write slug-marker.json + local-state.json (atomic 0o600).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXIT_DIRTY = 4
EXIT_UNSAFE_MANIFEST = 7
FETCH_CONCURRENCY = 8


class PullFileError(Exception):
    """Fetching or writing one pulled file failed; ``error`` is the original cause."""

    def __init__(self, rel: str, error: Exception) -> None:
        super().__init__(f"{rel}: {error}")
        self.rel = rel
        self.error = error


async def run_pull(
    slug: str,
    *,
    force: bool = False,
    target: Path | None = None,
) -> None:
    from keenyspace_shared.atomic_write import write_atomic
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    from keenyspace.clients.http import build_authed_http_client
    from keenyspace.fs.atomic import write_atomic_secret
    from keenyspace.paths import DEFAULT_PULL_ROOT, STATE_DIR
    from keenyspace.pull.manifest import (
        UnsafeManifestPathError,
        diff_manifests,
        hash_local_tree,
        resolve_vault_path,
    )
    from keenyspace.pull.stash import render_diff, stash_dirty

    console = Console()
    target_path = target or (DEFAULT_PULL_ROOT / slug)
    state_dir = STATE_DIR / slug
    state_dir.mkdir(parents=True, exist_ok=True)
    local_state_path = state_dir / "local-state.json"

    async with await build_authed_http_client() as client:
        resp = await client.get(f"/v1/api/workspaces/{slug}/manifest")
        resp.raise_for_status()
        server_doc: dict[str, Any] = resp.json()
        server_files: dict[str, str] = dict(server_doc.get("files") or {})
        try:
            dest_paths = {rel: resolve_vault_path(target_path, rel) for rel in server_files}
        except UnsafeManifestPathError as exc:
            console.print(
                "[red]Refusing to pull: manifest path resolves outside the vault "
                f"(traversal or symlink): {escape(str(exc))}[/red]"
            )
            sys.exit(EXIT_UNSAFE_MANIFEST)

        # A target dir that does not exist yet means "first pull" — no files
        # can be modified/added/removed relative to nothing, and the server's
        # manifest must NOT register every file as `removed`.
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

        async def fetch(rel: str) -> bytes:
            return await _fetch_page_bytes(client, slug, rel)

        async def map_or_abort[T](
            fn: Callable[[str], Awaitable[T]], rels: Iterable[str]
        ) -> dict[str, T]:
            try:
                return await _map_bounded(fn, rels)
            except PullFileError as exc:
                console.print(
                    f"[red]Pull aborted on {escape(exc.rel)}: {escape(str(exc.error))}[/red]"
                )
                raise exc.error from None

        stash_root: Path | None = None
        preloaded_server: dict[str, bytes] = {}
        if diff.is_dirty:
            iso = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
            stash_root = state_dir / "conflicts" / iso
            stash_root.mkdir(parents=True, exist_ok=True)
            stash_dirty(diff, target_path, stash_root)
            preloaded_server = await map_or_abort(fetch, diff.modified)
            render_diff(diff, target_path, lambda rel: preloaded_server[rel])

        target_path.mkdir(parents=True, exist_ok=True)

        async def pull_file(rel: str) -> str:
            payload = preloaded_server[rel] if rel in preloaded_server else await fetch(rel)
            await asyncio.to_thread(write_atomic, dest_paths[rel], payload)
            return "sha256:" + hashlib.sha256(payload).hexdigest()

        changed = [rel for rel, h in server_files.items() if local_files.get(rel) != h]
        written = await map_or_abort(pull_file, changed)
        # Record the sha256 of the bytes actually written to disk, not the
        # manifest hash captured at the start of the pull. If server canon
        # mutates between manifest fetch and per-file fetch (a compile pass
        # produces fresh content for one of the files), local-state.json must
        # reflect the bytes actually on disk so the next `pull` is not falsely
        # reported as dirty. Sorted so the file content does not depend on
        # fetch completion order.
        actual_hashes = {rel: written.get(rel, server_files[rel]) for rel in sorted(server_files)}

        for rel in set(local_files) - set(server_files):
            (target_path / rel).unlink(missing_ok=True)

        marker = target_path / ".keenyspace" / "slug-marker.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_secret(marker, json.dumps({"slug": slug}, indent=2).encode())

        new_manifest = {
            "version": 1,
            "workspace_slug": slug,
            "server_canon_at": server_doc.get("server_canon_at"),
            "last_pull_ts": datetime.now(UTC).isoformat(),
            "files": actual_hashes,
        }
        write_atomic_secret(local_state_path, json.dumps(new_manifest, indent=2).encode())

    console.print(f"[green]Pulled {len(server_files)} files to {target_path}[/green]")
    if stash_root is not None:
        console.print(f"[yellow]Dirty files stashed to {stash_root}[/yellow]")


async def _map_bounded[T](
    fn: Callable[[str], Awaitable[T]],
    rels: Iterable[str],
    *,
    limit: int = FETCH_CONCURRENCY,
) -> dict[str, T]:
    """Run ``fn`` over ``rels`` with at most ``limit`` in flight.

    The first failure cancels everything still pending and is raised as
    ``PullFileError`` naming the file.
    """
    semaphore = asyncio.Semaphore(limit)
    results: dict[str, T] = {}

    async def run(rel: str) -> None:
        async with semaphore:
            try:
                results[rel] = await fn(rel)
            except Exception as exc:
                raise PullFileError(rel, exc) from exc

    try:
        async with asyncio.TaskGroup() as tg:
            for rel in rels:
                tg.create_task(run(rel))
    except BaseExceptionGroup as group:
        first = next((e for e in group.exceptions if isinstance(e, PullFileError)), None)
        if first is None:
            raise
        raise first from None
    return results


async def _fetch_page_bytes(client: Any, slug: str, rel: str) -> bytes:
    resp = await client.get(f"/v1/api/workspaces/{slug}/pages-raw/{rel}")
    resp.raise_for_status()
    content: bytes = resp.content
    return content
