from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_create_workspace_returns_201(client, fs_root):
    try:
        resp = await client.post(
            "/v1/api/workspaces/",
            json={"slug": "scratch", "blueprint": "default"},
        )
    except Exception as exc:
        if "postgres" in str(exc).lower() or "connect" in str(exc).lower():
            pytest.skip(f"postgres unavailable: {exc}")
        raise

    if resp.status_code in (500, 503):
        body = resp.json()
        if "postgres" in str(body).lower() or "connect" in str(body).lower():
            pytest.skip("postgres unavailable")

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
    try:
        resp1 = await client.post(
            "/v1/api/workspaces/",
            json={"slug": "scratch2", "blueprint": "default"},
        )
        if resp1.status_code in (500, 503):
            pytest.skip("postgres unavailable")
        assert resp1.status_code == 201

        resp2 = await client.post(
            "/v1/api/workspaces/",
            json={"slug": "scratch2", "blueprint": "default"},
        )
        assert resp2.status_code == 409
    except Exception as exc:
        if "postgres" in str(exc).lower() or "connect" in str(exc).lower():
            pytest.skip(f"postgres unavailable: {exc}")
        raise
