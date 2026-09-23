"""Crash-safe file replacement shared by the server and the CLI client."""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path


def write_atomic(dest: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Write ``data`` to ``dest`` so readers never observe a partial file.

    The temporary file is created in ``dest``'s own directory — a rename across
    filesystems would degrade to a non-atomic copy. The file is fsynced before
    the rename and the parent directory afterwards, so the replacement survives
    a crash. The temporary file is removed if anything fails before the rename.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp.{secrets.token_hex(8)}"

    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise

    dir_fd = os.open(dest.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
