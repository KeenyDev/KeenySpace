from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path


def write_atomic(dest: Path, data: bytes, *, mode: int = 0o644) -> None:
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


def write_atomic_secret(dest: Path, data: bytes) -> None:
    write_atomic(dest, data, mode=0o600)
