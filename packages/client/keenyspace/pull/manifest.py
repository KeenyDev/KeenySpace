"""Local sha256 manifest + diff against server manifest.

Scope (D-13): .md anywhere + raw/ subtree. Files outside this scope are IGNORED —
they MUST NEVER be reported as `removed` or `added`. Pitfall #9: a stray
`notes.txt` in the vault root must not trigger a dirty state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

_EXCLUDED_TOP_LEVEL = frozenset({".obsidian", ".keenyspace", "logs", "tmp"})


@dataclass
class ManifestDiff:
    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def is_dirty(self) -> bool:
        return bool(self.modified or self.added or self.removed)


def hash_local_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if parts[0] in _EXCLUDED_TOP_LEVEL:
            continue
        if not (rel.endswith(".md") or parts[0] == "raw"):
            continue
        out[rel] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def diff_manifests(
    local: dict[str, str], server: dict[str, str]
) -> ManifestDiff:
    diff = ManifestDiff()
    for path, server_hash in server.items():
        if path not in local:
            diff.removed.append(path)
        elif local[path] != server_hash:
            diff.modified.append(path)
    for path in local:
        if path not in server:
            diff.added.append(path)
    diff.modified.sort()
    diff.added.sort()
    diff.removed.sort()
    return diff
