"""REST GET /v1/api/workspaces/<slug>/pages/<path> integration tests."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.test_workspace_manifest import (
    _reset_schema,
    _seed_api_key_post_lifespan,
    _seed_workspace,
    _workspace_dir,
)

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
]


async def test_get_page_returns_frontmatter_and_body_and_maps_errors(app, pg_url) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            slug = await _seed_workspace(client)
            ws_dir = _workspace_dir(app, slug)
            (ws_dir / "concepts").mkdir(parents=True, exist_ok=True)
            (ws_dir / "concepts" / "foo.md").write_text("---\ntitle: Foo\n---\n# Foo\nbody\n")

            resp = await client.get(f"/v1/api/workspaces/{slug}/pages/concepts/foo.md")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["path"] == "concepts/foo.md"
            assert data["frontmatter"] == {"title": "Foo"}
            assert data["content"] == "# Foo\nbody\n"

            missing = await client.get(f"/v1/api/workspaces/{slug}/pages/concepts/nope.md")
            assert missing.status_code == 404

            unsafe = await client.get(f"/v1/api/workspaces/{slug}/pages/.obsidian/app.json")
            assert unsafe.status_code == 400
