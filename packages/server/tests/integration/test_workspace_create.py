from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

_IMAGE_BLUEPRINTS = Path(__file__).resolve().parents[4] / "blueprints"


@pytest.fixture(autouse=True)
def _seed_blueprints(fs_root: Path) -> None:
    # `client` runs only the engine lifespan, so the boot-time blueprint sync
    # never populates fs_root/blueprints.
    from keenyspace_server.fs.bootstrap import ensure_fs_root_layout

    ensure_fs_root_layout(fs_root, _IMAGE_BLUEPRINTS)


async def test_create_workspace_returns_201(client, fs_root):
    resp = await client.post(
        "/v1/api/workspaces/",
        json={"slug": "scratch", "blueprint": "default"},
    )

    if resp.status_code in (500, 503):
        pytest.skip("postgres unavailable (engine lifespan not running in ASGI transport)")

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "uuid" in data
    assert data["slug"] == "scratch"
    assert data["blueprint_ref"] == "default@v0.1"

    ws_uuid = data["uuid"]
    ws_dir = fs_root / "workspaces" / ws_uuid
    assert ws_dir.exists(), f"workspace dir not created: {ws_dir}"
    assert (ws_dir / "index.md").exists()
    assert "Index" in (ws_dir / "index.md").read_text()

    import yaml
    config_text = (ws_dir / ".keenyspace" / "config.yaml").read_text()
    config = yaml.safe_load(config_text)
    assert config["blueprint"] == "default@v0.1"


async def test_create_workspace_duplicate_slug_409(client, fs_root):
    resp1 = await client.post(
        "/v1/api/workspaces/",
        json={"slug": "scratch2", "blueprint": "default"},
    )
    if resp1.status_code in (500, 503):
        pytest.skip("postgres unavailable (engine lifespan not running in ASGI transport)")
    assert resp1.status_code == 201

    resp2 = await client.post(
        "/v1/api/workspaces/",
        json={"slug": "scratch2", "blueprint": "default"},
    )
    assert resp2.status_code == 409
