from __future__ import annotations

import bisect
from pathlib import Path

from keenyspace_server.ws.scan import iter_md_files
from keenyspace_server.ws.thread_slots import LoopLocalSemaphore

VAULT_SCAN_SLOTS = LoopLocalSemaphore(2)


def list_md_paths(ws_root: Path, prefix: str | None = None) -> list[str]:
    if not ws_root.is_dir():
        return []
    rels: list[str] = []
    for _abs, rel in iter_md_files(ws_root):
        rel_str = rel.as_posix()
        if prefix is not None and not rel_str.startswith(prefix):
            continue
        rels.append(rel_str)
    rels.sort()
    return rels


def search_workspace_files(
    ws_root: Path,
    query: str,
    *,
    after: str | None = None,
    skip: int = 0,
    limit: int | None = None,
) -> list[str]:
    """Case-insensitive literal substring search over filenames + content.

    Paths are listed and sorted by metadata first; file contents are then
    read in path order starting strictly after `after`, and the scan stops
    once `skip + limit` matches are found, so a page costs roughly the bytes
    up to its last match rather than the whole vault. The first `skip`
    matches are discarded.

    Per WR-08, regex semantics opened a ReDoS surface; the MCP-05 contract
    only promises literal-substring matching.
    """
    paths = list_md_paths(ws_root)
    start = 0 if after is None else bisect.bisect_right(paths, after)
    wanted = None if limit is None else skip + limit
    needle = query.lower()
    matches: list[str] = []
    for rel_str in paths[start:]:
        if wanted is not None and len(matches) >= wanted:
            break
        if needle in rel_str.lower():
            matches.append(rel_str)
            continue
        try:
            content = (ws_root / rel_str).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        if needle in content.lower():
            matches.append(rel_str)
    return matches[skip:]
