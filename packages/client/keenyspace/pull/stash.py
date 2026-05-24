"""Stash dirty files to ~/.local/state/keenyspace/<slug>/conflicts/<iso>/ and
emit a unified diff via rich.Syntax. Per 05-RESEARCH §12: splitlines(keepends=True)
is critical — without it difflib.unified_diff produces broken output."""

from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keenyspace.pull.manifest import ManifestDiff


def stash_dirty(diff: ManifestDiff, vault_root: Path, stash_root: Path) -> None:
    for rel in diff.modified + diff.added:
        src = vault_root / rel
        if not src.is_file():
            continue
        dst = stash_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


def render_diff(
    diff: ManifestDiff,
    vault_root: Path,
    fetch_server_bytes: Callable[[str], bytes],
) -> str:
    from rich.console import Console
    from rich.syntax import Syntax

    console: Any = Console()
    accumulated: list[str] = []
    for rel in diff.modified:
        local_path = vault_root / rel
        if not local_path.is_file():
            continue
        try:
            local_text = local_path.read_text(encoding="utf-8")
            server_text = fetch_server_bytes(rel).decode("utf-8")
        except UnicodeDecodeError:
            console.print(f"[yellow]Binary file diff skipped: {rel}[/yellow]")
            continue
        lines = list(
            difflib.unified_diff(
                local_text.splitlines(keepends=True),
                server_text.splitlines(keepends=True),
                fromfile=f"local/{rel}",
                tofile=f"server/{rel}",
                n=3,
            )
        )
        text = "".join(lines)
        if not text:
            continue
        accumulated.append(text)
        console.print(Syntax(text, "diff", theme="monokai", line_numbers=False))
    return "".join(accumulated)
