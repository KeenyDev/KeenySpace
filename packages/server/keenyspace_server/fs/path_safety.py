from __future__ import annotations

import errno as _errno
import os
from pathlib import Path


class UnsafePathError(ValueError):
    pass


UnsafePath = UnsafePathError


def validate_relative_path(page_path: str) -> str:
    if not page_path:
        raise UnsafePath("Path must not be empty")

    if len(page_path) > 512:
        raise UnsafePath("Path exceeds maximum length of 512 characters")

    if "\x00" in page_path:
        raise UnsafePath("Path contains NUL byte")

    if page_path.startswith("/") or page_path.startswith("\\"):
        raise UnsafePath("Path must not start with / or \\")

    parts = Path(page_path).parts
    for part in parts:
        if part in (".", ".."):
            raise UnsafePath(f"Path contains dot-segment: {part!r}")
        if part.startswith("."):
            raise UnsafePath(f"Path contains hidden component: {part!r}")

    if not page_path.endswith(".md"):
        page_path = page_path + ".md"

    return page_path


def open_workspace_page(ws_root: Path, page_path: str) -> tuple[int, Path]:
    canonical_path = validate_relative_path(page_path)

    target = (ws_root / canonical_path).resolve()
    ws_root_resolved = ws_root.resolve()

    if not target.is_relative_to(ws_root_resolved):
        raise UnsafePath(
            f"Path {page_path!r} resolves outside workspace root"
        )

    # O_NOFOLLOW guards only the final path component against symlinks.
    # resolve() above follows intermediate symlinks — a TOCTOU window exists
    # if an attacker can replace an intermediate directory component between
    # resolve() and os.open(). The practical risk is low: workspace directories
    # are not writable by untrusted users in v1 (single-org, single-process).
    # Full mitigation would require openat(2) with O_PATH|O_NOFOLLOW on each
    # component — deferred to v1.5 when multi-tenant isolation is added.
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == _errno.ELOOP:
            raise UnsafePath(f"Path {page_path!r} is a symlink") from exc
        raise

    return fd, target


_COMPILE_DENYLIST_PREFIXES: tuple[str, ...] = (
    ".keenyspace/",
    "logs/",
    "_templates/",
    "raw/",
)
_COMPILE_DENYLIST_EXACT: frozenset[str] = frozenset({"CLAUDE.md"})


def is_compile_writable(ws_root: Path, path: str) -> bool:
    """Return True iff `path` is a workspace-relative page the compile coordinator may write.

    Refuses anything that fails the 4-layer pre-validation OR matches the D-07 denylist
    ('.keenyspace/', 'logs/', '_templates/', 'raw/' prefixes; 'CLAUDE.md' exact).
    Case-folded before prefix comparison so 'Logs/foo.md' and 'logs/foo.md' both match.
    """
    try:
        canonical = validate_relative_path(path)
    except UnsafePath:
        return False
    canonical_lower = canonical.casefold()
    for prefix in _COMPILE_DENYLIST_PREFIXES:
        if canonical_lower.startswith(prefix):
            return False
    return canonical_lower not in {e.casefold() for e in _COMPILE_DENYLIST_EXACT}
