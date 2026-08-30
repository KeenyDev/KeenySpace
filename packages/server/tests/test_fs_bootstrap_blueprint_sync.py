"""Phase 4 UAT G-3 regression: blueprint catalog merge-on-boot.

ensure_fs_root_layout previously only seeded fs_root/blueprints/default/ on
first boot. Phase 4 Plan 01 added _instructions/ingest.md to the image
blueprint, but existing fs_root volumes never received the file, breaking
MCP get_instructions on upgrade-deploys. _merge_blueprint_tree fills the
gap with idempotent skip-on-exists semantics.

Skip-on-exists alone then let *changed* shipped files drift: commit 857b7c2
widened the ingest tool_whitelist, but every existing volume kept serving the
narrow pre-857b7c2 default forever. The sync manifest records what the image
last shipped so an untouched default can be upgraded while operator edits stay
untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from keenyspace_server.fs.bootstrap import (
    BLUEPRINT_SYNC_MANIFEST,
    _merge_blueprint_tree,
    ensure_fs_root_layout,
)


def _seed_image(image_dir: Path) -> None:
    """Build a minimal image-side blueprints/default/ tree."""
    blueprint = image_dir / "default"
    (blueprint / "_instructions").mkdir(parents=True)
    (blueprint / "_templates").mkdir()
    (blueprint / ".keenyspace").mkdir()
    (blueprint / "index.md").write_text("# image index\n")
    (blueprint / "CLAUDE.md").write_text("# image CLAUDE\n")
    (blueprint / "_instructions" / "ingest.md").write_text(
        "---\nname: ingest\n---\nbody\n"
    )
    (blueprint / "_templates" / "concept.md").write_text("# tpl\n")
    (blueprint / ".keenyspace" / "blueprint.yaml").write_text(
        "name: default\nversion: v0.1\n"
    )


def test_first_boot_clones_full_tree(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    ensure_fs_root_layout(fs_root, image_dir)

    assert (
        fs_root / "blueprints" / "default" / "index.md"
    ).read_text() == "# image index\n"
    assert (
        fs_root / "blueprints" / "default" / "_instructions" / "ingest.md"
    ).exists()
    assert (fs_root / "workspaces").is_dir()
    assert (fs_root / ".tmp").is_dir()


def test_second_boot_adds_missing_file_without_overwriting(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    # Simulate an existing-volume deploy: target exists, has operator-edited
    # index.md, but lacks _instructions/ingest.md (the new-in-Plan-01 file).
    target = fs_root / "blueprints" / "default"
    target.mkdir(parents=True)
    (target / "index.md").write_text("# OPERATOR EDIT\n")
    (target / ".keenyspace").mkdir()
    (target / ".keenyspace" / "blueprint.yaml").write_text(
        "name: default\nversion: v0.1\n"
    )

    ensure_fs_root_layout(fs_root, image_dir)

    # New file landed:
    assert (target / "_instructions" / "ingest.md").exists()
    # Operator file preserved:
    assert (target / "index.md").read_text() == "# OPERATOR EDIT\n"


def test_idempotent_no_change_on_repeated_boot(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    ensure_fs_root_layout(fs_root, image_dir)

    target = fs_root / "blueprints" / "default" / "_instructions" / "ingest.md"
    first_mtime = target.stat().st_mtime_ns
    # Bump fs clock granularity safety
    os.utime(target, ns=(first_mtime, first_mtime))

    ensure_fs_root_layout(fs_root, image_dir)

    assert target.stat().st_mtime_ns == first_mtime


def test_workspaces_dir_untouched(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    # Pre-seed a workspace dir that should never be touched by the merge.
    ws_uuid = "11111111-1111-1111-1111-111111111111"
    ws_dir = fs_root / "workspaces" / ws_uuid
    ws_dir.mkdir(parents=True)
    (ws_dir / "CLAUDE.md").write_text("# operator workspace text\n")

    ensure_fs_root_layout(fs_root, image_dir)

    assert (ws_dir / "CLAUDE.md").read_text() == "# operator workspace text\n"


def test_merge_skips_symlinks_in_image(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    # Add a symlink inside the image blueprint to a target outside it.
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    link = image_dir / "default" / "evil-link.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")

    # Pre-create target dir with all NON-symlink files (so we exercise the
    # merge branch, not the first-boot copytree).
    target = fs_root / "blueprints" / "default"
    target.mkdir(parents=True)
    (target / "index.md").write_text("# operator\n")

    ensure_fs_root_layout(fs_root, image_dir)

    assert not (target / "evil-link.md").exists()
    assert not (target / "evil-link.md").is_symlink()


def test_merge_oserror_logs_and_continues(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("a\n")
    (src / "b.md").write_text("b\n")
    dst = tmp_path / "dst"
    dst.mkdir()

    real_copy = shutil.copy2

    def flaky_copy(s, d, *args, **kwargs):
        if str(s).endswith("a.md"):
            raise OSError("simulated")
        return real_copy(s, d, *args, **kwargs)

    with patch(
        "keenyspace_server.fs.bootstrap.shutil.copy2", side_effect=flaky_copy
    ):
        _merge_blueprint_tree(src, dst)  # must NOT raise

    assert (dst / "b.md").exists()
    assert not (dst / "a.md").exists()


def _manifest(fs_root: Path) -> dict:
    return json.loads(
        (fs_root / "blueprints" / BLUEPRINT_SYNC_MANIFEST).read_bytes()
    )


def test_first_boot_records_shipped_digests(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    ensure_fs_root_layout(fs_root, image_dir)

    manifest = _manifest(fs_root)
    assert manifest["schema_version"] == 1
    files = manifest["blueprints"]["default"]
    assert "_instructions/ingest.md" in files
    assert "index.md" in files
    # The manifest lives beside the blueprint dirs, not inside one, so
    # clone_default_blueprint never copies it into a workspace vault.
    assert not (fs_root / "blueprints" / "default" / BLUEPRINT_SYNC_MANIFEST).exists()


def test_untouched_shipped_file_upgrades_when_image_changes(tmp_path: Path) -> None:
    """The 857b7c2 case: on-disk copy is a pristine default, image moved on."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)
    ingest_src = image_dir / "default" / "_instructions" / "ingest.md"
    ingest_dst = fs_root / "blueprints" / "default" / "_instructions" / "ingest.md"

    ensure_fs_root_layout(fs_root, image_dir)
    assert ingest_dst.read_text() == "---\nname: ingest\n---\nbody\n"

    # Image upgrade widens the whitelist.
    ingest_src.write_text("---\nname: ingest\ntool_whitelist: [read_page]\n---\nbody\n")
    ensure_fs_root_layout(fs_root, image_dir)

    assert ingest_dst.read_text() == ingest_src.read_text()
    assert (
        _manifest(fs_root)["blueprints"]["default"]["_instructions/ingest.md"]
        == hashlib.sha256(ingest_src.read_bytes()).hexdigest()
    )


def test_operator_edit_survives_image_change(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    ensure_fs_root_layout(fs_root, image_dir)

    index_dst = fs_root / "blueprints" / "default" / "index.md"
    index_dst.write_text("# OPERATOR EDIT\n")
    (image_dir / "default" / "index.md").write_text("# image index v2\n")

    ensure_fs_root_layout(fs_root, image_dir)

    assert index_dst.read_text() == "# OPERATOR EDIT\n"


def test_operator_edit_still_preserved_when_manifest_lost(tmp_path: Path) -> None:
    """No manifest means no provenance, so fall back to skip-on-exists."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    ensure_fs_root_layout(fs_root, image_dir)
    (fs_root / "blueprints" / BLUEPRINT_SYNC_MANIFEST).unlink()

    index_dst = fs_root / "blueprints" / "default" / "index.md"
    index_dst.write_text("# OPERATOR EDIT\n")
    (image_dir / "default" / "index.md").write_text("# image index v2\n")

    ensure_fs_root_layout(fs_root, image_dir)

    assert index_dst.read_text() == "# OPERATOR EDIT\n"


def test_corrupt_manifest_does_not_break_boot(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    ensure_fs_root_layout(fs_root, image_dir)
    (fs_root / "blueprints" / BLUEPRINT_SYNC_MANIFEST).write_text("{not json")

    ensure_fs_root_layout(fs_root, image_dir)

    assert (fs_root / "blueprints" / "default" / "index.md").exists()
    assert _manifest(fs_root)["blueprints"]["default"]


def test_in_sync_file_heals_missing_manifest_entry(tmp_path: Path) -> None:
    """A volume matching the image is recorded, so the next upgrade applies."""
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    # Pre-manifest volume: content identical to the image, no manifest at all.
    shutil.copytree(image_dir / "default", fs_root / "blueprints" / "default")

    ensure_fs_root_layout(fs_root, image_dir)
    assert "index.md" in _manifest(fs_root)["blueprints"]["default"]

    (image_dir / "default" / "index.md").write_text("# image index v2\n")
    ensure_fs_root_layout(fs_root, image_dir)

    assert (
        fs_root / "blueprints" / "default" / "index.md"
    ).read_text() == "# image index v2\n"


def test_merge_called_before_sweep_stale_tmp(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    (fs_root / ".tmp").mkdir()
    stale = fs_root / ".tmp" / "import_deadbeef"
    stale.mkdir()
    (stale / "junk").write_text("x")

    image_dir = tmp_path / "image"
    image_dir.mkdir()
    _seed_image(image_dir)

    target = fs_root / "blueprints" / "default"
    target.mkdir(parents=True)

    ensure_fs_root_layout(fs_root, image_dir)

    # New blueprint file landed:
    assert (target / "_instructions" / "ingest.md").exists()
    # Stale tmp reaped:
    assert not stale.exists()
