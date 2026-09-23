from __future__ import annotations

from pathlib import Path

import pytest
from keenyspace_shared.atomic_write import write_atomic


def test_write_creates_file(tmp_path: Path) -> None:
    dest = tmp_path / "page.md"
    write_atomic(dest, b"hello world")
    assert dest.read_bytes() == b"hello world"


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "sub" / "dir" / "page.md"
    write_atomic(dest, b"content")
    assert dest.read_bytes() == b"content"


def test_no_tmp_file_remains_after_success(tmp_path: Path) -> None:
    dest = tmp_path / "page.md"
    write_atomic(dest, b"data")
    tmp_files = list(tmp_path.glob(".*.tmp.*"))
    assert tmp_files == [], f"Unexpected tmp files: {tmp_files}"


def test_tmp_in_same_dir_as_dest(tmp_path: Path) -> None:
    dest = tmp_path / "page.md"
    write_atomic(dest, b"test")

    assert dest.exists()
    assert dest.read_bytes() == b"test"
    tmp_files = list(tmp_path.glob(".*.tmp.*"))
    assert tmp_files == [], f"tmp file should be sibling and cleaned up: {tmp_files}"


def test_overwrites_existing_file(tmp_path: Path) -> None:
    dest = tmp_path / "page.md"
    dest.write_bytes(b"old content")
    write_atomic(dest, b"new content")
    assert dest.read_bytes() == b"new content"


def test_no_tmp_remains_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    dest = tmp_path / "page.md"
    with pytest.raises(OSError, match="simulated failure"):
        write_atomic(dest, b"data")

    tmp_files = list(tmp_path.glob(".*.tmp.*"))
    assert tmp_files == [], f"Tmp file not cleaned up: {tmp_files}"
