"""Local sha256 manifest + diff against server manifest.

Scope: .md anywhere plus the raw/ subtree. Files outside this scope are
IGNORED — they MUST NEVER be reported as `removed` or `added`; a stray
`notes.txt` in the vault root must not put the vault in a dirty state.
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
        with path.open("rb") as fh:
            out[rel] = "sha256:" + hashlib.file_digest(fh, "sha256").hexdigest()
    return out


class UnsafeManifestPathError(ValueError):
    """A manifest key that would resolve outside the vault root."""


def resolve_vault_path(root: Path, rel: str) -> Path:
    """Map a server-supplied manifest key to a path strictly inside ``root``.

    Manifest keys are untrusted input (malicious server / MITM): an absolute key
    or a ``..`` segment would otherwise let a pull overwrite arbitrary files such
    as ``~/.zshrc``. Resolving both sides also refuses writes through a symlink
    that points outside the vault.
    """
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/"):
        raise UnsafeManifestPathError(rel)
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise UnsafeManifestPathError(rel)
    candidate = root / rel
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise UnsafeManifestPathError(rel)
    return candidate


def diff_manifests(local: dict[str, str], server: dict[str, str]) -> ManifestDiff:
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
