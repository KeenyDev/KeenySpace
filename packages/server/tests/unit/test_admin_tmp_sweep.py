"""Startup sweep of admin scratch under fs_root/tmp."""

from __future__ import annotations

from pathlib import Path

from keenyspace_server.fs.bootstrap import ensure_fs_root_layout


def test_startup_sweeps_backup_scratch_but_keeps_restore_aside(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs_root"
    stale_backup = fs_root / "tmp" / "backup-deadbeef"
    stale_backup.mkdir(parents=True)
    (stale_backup / "backup.tar.gz").write_bytes(b"x" * 1024)
    aside = fs_root / "tmp" / "restore-cafebabe.aside"
    (aside / "0").mkdir(parents=True)
    (aside / "0" / "index.md").write_text("old workspace\n")

    ensure_fs_root_layout(fs_root, tmp_path / "no-image-blueprints")

    assert not stale_backup.exists()
    assert (aside / "0" / "index.md").read_text() == "old workspace\n"
