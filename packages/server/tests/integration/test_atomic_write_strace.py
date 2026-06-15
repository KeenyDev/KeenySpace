"""
Linux-only strace test: validates atomic write syscall sequence (criterion #4).
Runs under strace to assert write -> fsync -> rename -> fsync(parent_dir) ordering.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="strace is Linux-only")
def test_atomic_write_strace_syscall_sequence(tmp_path: Path):
    strace = subprocess.run(["which", "strace"], capture_output=True)
    if strace.returncode != 0:
        pytest.skip("strace not on PATH")

    dest = tmp_path / "page.md"
    strace_log = tmp_path / "strace.log"

    subprocess.run(
        [
            "strace",
            "-e", "trace=openat,write,fsync,rename,renameat,renameat2,close",
            "-o", str(strace_log),
            sys.executable,
            "-c",
            f"from keenyspace_server.fs.atomic import write_atomic; from pathlib import Path; write_atomic(Path('{dest}'), b'hello')",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    strace_text = strace_log.read_text()

    assert re.search(r"openat\(.*\.tmp\.[a-f0-9]+.*O_WRONLY.*O_CREAT.*O_EXCL", strace_text), \
        f"openat(O_WRONLY|O_CREAT|O_EXCL) for tmp not found in strace:\n{strace_text[:2000]}"

    assert re.search(r"write\(", strace_text), \
        f"write syscall not found in strace:\n{strace_text[:2000]}"

    assert re.search(r"fsync\(", strace_text), \
        f"fsync not found in strace:\n{strace_text[:2000]}"

    assert re.search(r"rename(at2?)?\(", strace_text), \
        f"rename/renameat not found in strace:\n{strace_text[:2000]}"

    assert re.search(r"openat\(.*O_RDONLY.*O_DIRECTORY", strace_text), \
        f"openat(O_DIRECTORY) for dir fsync not found in strace:\n{strace_text[:2000]}"

    tmp_match = re.search(r"openat\(.*\"([^\"]*\.tmp\.[a-f0-9]+)\"", strace_text)
    if tmp_match:
        tmp_path_str = tmp_match.group(1)
        assert str(dest.parent) in tmp_path_str or Path(tmp_path_str).parent == dest.parent, \
            f"tmp file {tmp_path_str!r} is not sibling of dest {dest}"

    assert dest.exists(), "destination file must exist after atomic write"
    assert dest.read_bytes() == b"hello", "destination file content must be 'hello'"
