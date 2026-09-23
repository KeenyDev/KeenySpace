from __future__ import annotations

from pathlib import Path

from keenyspace_shared.atomic_write import write_atomic


def write_atomic_secret(dest: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``dest`` with owner-only permissions."""
    write_atomic(dest, data, mode=0o600)
