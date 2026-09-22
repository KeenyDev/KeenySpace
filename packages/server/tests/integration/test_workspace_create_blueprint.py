"""POST /v1/api/workspaces/ refuses blueprint names that are not a catalog entry."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

_IMAGE_BLUEPRINTS = Path(__file__).resolve().parents[4] / "blueprints"


@pytest.fixture(autouse=True)
def _seed_blueprints(fs_root: Path) -> None:
    from keenyspace_server.fs.bootstrap import ensure_fs_root_layout

    ensure_fs_root_layout(fs_root, _IMAGE_BLUEPRINTS)


@pytest.mark.parametrize(
    "blueprint",
    [
        pytest.param("/etc", id="absolute"),
        pytest.param("..", id="parent"),
        pytest.param("../x", id="parent-relative"),
        pytest.param("../workspaces", id="sibling-tree"),
        pytest.param("a/b", id="nested"),
        pytest.param("Default", id="uppercase"),
        pytest.param("", id="empty"),
    ],
)
async def test_create_workspace_rejects_unsafe_blueprint_name(
    client, fs_root: Path, blueprint: str
) -> None:
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": "unsafe-bp", "blueprint": blueprint}
    )

    assert resp.status_code == 422, resp.text
    assert list((fs_root / "workspaces").iterdir()) == []


async def test_create_workspace_unknown_blueprint_returns_422(
    client, fs_root: Path
) -> None:
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": "missing-bp", "blueprint": "nope"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "unknown blueprint 'nope'"
    assert list((fs_root / "workspaces").iterdir()) == []
    info = await client.get("/v1/api/workspaces/missing-bp")
    assert info.status_code == 404, info.text


async def test_create_workspace_blueprint_symlinked_outside_catalog_returns_422(
    client, fs_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("server-side file\n")
    (fs_root / "blueprints" / "escape").symlink_to(outside, target_is_directory=True)

    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": "escape-bp", "blueprint": "escape"}
    )

    assert resp.status_code == 422, resp.text
    assert list((fs_root / "workspaces").iterdir()) == []
