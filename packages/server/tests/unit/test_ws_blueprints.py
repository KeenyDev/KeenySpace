from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml


@pytest.mark.asyncio
async def test_returns_empty_when_blueprints_dir_missing(tmp_path: Path) -> None:
    from keenyspace_server.ws.blueprints import list_blueprints_from_fs

    result = await list_blueprints_from_fs(tmp_path)
    assert result == []


@pytest.mark.asyncio
async def test_discovers_default_blueprint(tmp_path: Path) -> None:
    from keenyspace_server.ws.blueprints import list_blueprints_from_fs

    bp_dir = tmp_path / "blueprints" / "default" / ".keenyspace"
    bp_dir.mkdir(parents=True)
    (bp_dir / "blueprint.yaml").write_text(
        yaml.dump({"version": "v0.1", "description": "Default blueprint"})
    )

    result = await list_blueprints_from_fs(tmp_path)
    assert len(result) == 1
    assert result[0].name == "default"
    assert result[0].version == "v0.1"
    assert result[0].description == "Default blueprint"


@pytest.mark.asyncio
async def test_discovers_multiple_blueprints_sorted_by_name(tmp_path: Path) -> None:
    from keenyspace_server.ws.blueprints import list_blueprints_from_fs

    for name in ("test-bp", "default", "aaa-bp"):
        bp_dir = tmp_path / "blueprints" / name / ".keenyspace"
        bp_dir.mkdir(parents=True)
        (bp_dir / "blueprint.yaml").write_text(
            yaml.dump({"version": "v0.1", "description": f"{name} blueprint"})
        )

    result = await list_blueprints_from_fs(tmp_path)
    assert [b.name for b in result] == ["aaa-bp", "default", "test-bp"]


@pytest.mark.asyncio
async def test_skips_subdir_without_blueprint_yaml(tmp_path: Path) -> None:
    from keenyspace_server.ws.blueprints import list_blueprints_from_fs

    no_yaml_dir = tmp_path / "blueprints" / "no-yaml"
    no_yaml_dir.mkdir(parents=True)
    (no_yaml_dir / "CLAUDE.md").write_text("# no yaml here\n")

    result = await list_blueprints_from_fs(tmp_path)
    assert result == []


@pytest.mark.asyncio
async def test_skips_non_dir_entries(tmp_path: Path) -> None:
    from keenyspace_server.ws.blueprints import list_blueprints_from_fs

    (tmp_path / "blueprints").mkdir(parents=True)
    (tmp_path / "blueprints" / ".DS_Store").write_text("")

    result = await list_blueprints_from_fs(tmp_path)
    assert result == []


@pytest.mark.asyncio
async def test_logs_and_skips_malformed_yaml(tmp_path: Path) -> None:
    from keenyspace_server.ws.blueprints import list_blueprints_from_fs

    bp_dir = tmp_path / "blueprints" / "bad-bp" / ".keenyspace"
    bp_dir.mkdir(parents=True)
    (bp_dir / "blueprint.yaml").write_text("{not: valid yaml: [\n")

    with structlog.testing.capture_logs() as captured:
        result = await list_blueprints_from_fs(tmp_path)

    assert result == []
    assert any(
        event.get("event") == "blueprint.yaml_parse_failed" for event in captured
    )


@pytest.mark.asyncio
async def test_logs_and_skips_non_dict_yaml(tmp_path: Path) -> None:
    from keenyspace_server.ws.blueprints import list_blueprints_from_fs

    bp_dir = tmp_path / "blueprints" / "list-bp" / ".keenyspace"
    bp_dir.mkdir(parents=True)
    (bp_dir / "blueprint.yaml").write_text(yaml.dump(["array", "not", "dict"]))

    with structlog.testing.capture_logs() as captured:
        result = await list_blueprints_from_fs(tmp_path)

    assert result == []
    assert any(
        event.get("event") == "blueprint.yaml_invalid_shape" for event in captured
    )
