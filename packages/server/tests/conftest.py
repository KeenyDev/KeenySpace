from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def fs_root(tmp_path):
    root = tmp_path / "fs_root"
    root.mkdir()
    return root


@pytest.fixture
def pg_url():
    url = os.environ.get(
        "KEENYSPACE_DB__URL",
        "postgresql+asyncpg://postgres:x@localhost:55432/postgres",
    )
    return url


@pytest.fixture
def app_env(fs_root, pg_url, monkeypatch):
    monkeypatch.setenv("KEENYSPACE_DB__URL", pg_url)
    monkeypatch.setenv("KEENYSPACE_FS__ROOT", str(fs_root))
    monkeypatch.setenv("KEENYSPACE_AUTH__DEV_TOKEN", "test")
    return {"fs_root": fs_root, "pg_url": pg_url}


@pytest.fixture
def app(app_env):
    import importlib
    import keenyspace_server.config as cfg_module
    cfg_module.get_settings.cache_clear()

    from keenyspace_server.main import build_app

    application = build_app()
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-test"},
    ) as c:
        yield c
