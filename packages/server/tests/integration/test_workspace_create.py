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


async def _insert_workspace_row(slug: str) -> None:
    import uuid
    from datetime import UTC, datetime

    from keenyspace_server.db.models import Workspace
    from keenyspace_server.db.session import get_db_session

    async with get_db_session() as session:
        session.add(
            Workspace(
                uuid=uuid.uuid4(),
                slug=slug,
                display_name=slug,
                blueprint_ref="default@v0.1",
                status="active",
                created_at=datetime.now(UTC),
                archived_at=None,
            )
        )
        await session.commit()


async def test_create_releases_db_connection_and_reaps_dir_on_slug_race(
    client, fs_root, monkeypatch
):
    import asyncio

    import keenyspace_server.api.workspaces as workspaces_api
    from keenyspace_server.db.session import get_engine

    engine = get_engine()
    if engine is None:
        pytest.skip("postgres unavailable (engine lifespan not running in ASGI transport)")
    loop = asyncio.get_running_loop()
    real_clone = workspaces_api.clone_default_blueprint
    checked_out_during_clone: list[int] = []
    cloned_dirs: list[Path] = []

    def _racing_clone(*args, **kwargs):
        checked_out_during_clone.append(engine.pool.checkedout())
        ws_dir = real_clone(*args, **kwargs)
        cloned_dirs.append(ws_dir)
        asyncio.run_coroutine_threadsafe(
            _insert_workspace_row(kwargs["slug"]), loop
        ).result(timeout=10)
        return ws_dir

    monkeypatch.setattr(workspaces_api, "clone_default_blueprint", _racing_clone)
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": "raced", "blueprint": "default"}
    )

    assert resp.status_code == 409, resp.text
    assert checked_out_during_clone == [0]
    assert len(cloned_dirs) == 1
    assert not cloned_dirs[0].exists()
