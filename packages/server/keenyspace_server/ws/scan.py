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
