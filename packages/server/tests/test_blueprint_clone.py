from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml


def _make_blueprint(bp_dir: Path, *, with_instructions: bool = True) -> None:
    keenyspace_dir = bp_dir / ".keenyspace"
    keenyspace_dir.mkdir(parents=True)
    (keenyspace_dir / "blueprint.yaml").write_text(
        yaml.dump({"version": "v0.1", "description": "test blueprint"})
    )
    (bp_dir / "index.md").write_text("# Index\n")
    if with_instructions:
        instructions_dir = bp_dir / "_instructions"
        instructions_dir.mkdir()
        (instructions_dir / "ingest.md").write_text("---\ntool_whitelist: []\n---\nHello.\n")


def test_clone_moves_instructions_dir(tmp_path: Path) -> None:
    from keenyspace_server.fs.blueprint import clone_default_blueprint

    fs_root = tmp_path / "fs_root"
    bp_dir = fs_root / "blueprints" / "test-bp"
    _make_blueprint(bp_dir, with_instructions=True)

    ws_uuid = uuid.uuid4()
    ws_dir = clone_default_blueprint(fs_root, "test-bp", ws_uuid, slug="test", display_name="test")

    assert (ws_dir / ".keenyspace" / "instructions" / "ingest.md").exists()
    assert not (ws_dir / "_instructions").exists()


def test_clone_no_instructions_dir_ok(tmp_path: Path) -> None:
    from keenyspace_server.fs.blueprint import clone_default_blueprint

    fs_root = tmp_path / "fs_root"
    bp_dir = fs_root / "blueprints" / "test-bp"
    _make_blueprint(bp_dir, with_instructions=False)

    ws_uuid = uuid.uuid4()
    ws_dir = clone_default_blueprint(fs_root, "test-bp", ws_uuid, slug="test", display_name="test")

    assert not (ws_dir / ".keenyspace" / "instructions").exists()
    assert not (ws_dir / "_instructions").exists()


def test_clone_writes_config_yaml(tmp_path: Path) -> None:
    from keenyspace_server.fs.blueprint import clone_default_blueprint

    fs_root = tmp_path / "fs_root"
    bp_dir = fs_root / "blueprints" / "test-bp"
    _make_blueprint(bp_dir, with_instructions=False)

    ws_uuid = uuid.uuid4()
    ws_dir = clone_default_blueprint(fs_root, "test-bp", ws_uuid, slug="myslug", display_name="test")

    config_path = ws_dir / ".keenyspace" / "config.yaml"
    assert config_path.exists()
    config = yaml.safe_load(config_path.read_text())
    assert config["blueprint"] == "test-bp@v0.1"
    assert config["slug"] == "myslug"
