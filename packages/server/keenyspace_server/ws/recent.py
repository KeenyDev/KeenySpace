from __future__ import annotations

from pathlib import Path

from keenyspace_server.ws.scan import iter_md_files


def scan_recent_changes(
    ws_root: Path, since_ns: int | None = None
) -> list[tuple[int, str]]:
    if not ws_root.is_dir():
        return []
    results: list[tuple[int, str]] = []
    for abs_path, rel in iter_md_files(ws_root):
        try:
            mtime_ns = abs_path.stat().st_mtime_ns
        except OSError:
            continue
        if since_ns is not None and mtime_ns < since_ns:
            continue
        results.append((mtime_ns, rel.as_posix()))
    results.sort(key=lambda item: (-item[0], item[1]))
    return results
