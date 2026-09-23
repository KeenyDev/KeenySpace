from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

_SCAN_SKIP: frozenset[str] = frozenset({".keenyspace", ".obsidian", "logs"})


def iter_md_files(ws_dir: Path) -> Iterator[tuple[Path, Path]]:
    for f in ws_dir.rglob("*.md"):
        try:
            rel = f.relative_to(ws_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in _SCAN_SKIP:
            continue
        yield f, rel


def count_pages(ws_dir: Path) -> int:
    """Count the markdown pages under ``ws_dir``; 0 when the directory is absent.

    Blocking filesystem walk — call it from a thread on async paths.
    """
    if not ws_dir.is_dir():
        return 0
    return sum(1 for _ in iter_md_files(ws_dir))
