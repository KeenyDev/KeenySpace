"""Workspace manifest endpoint integration tests.

Endpoint: GET /v1/api/workspaces/<slug>/manifest -> {files: {path: sha256:<hex>}, server_canon_at}.

Manifest scope is .md anywhere plus the raw/ subtree only; the .obsidian, .keenyspace,
logs and tmp top-level directories MUST be excluded.
"""

from __future__ import annotations

import hashlib
import os

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import _reset_schema
from tests.integration.conftest import (
    _seed_api_key_post_lifespan,
    _seed_workspace,
    _workspace_dir,
)

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
]


async def test_manifest_returns_md_and_raw(app, pg_url) -> None:
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
            (ws_dir / "concepts" / "foo.md").write_bytes(b"foo body\n")
            (ws_dir / "raw").mkdir(parents=True, exist_ok=True)
            (ws_dir / "raw" / "img.png").write_bytes(b"\x89PNG\r\n\x1a\nbinary")
            (ws_dir / "logs").mkdir(parents=True, exist_ok=True)
            (ws_dir / "logs" / "2026.md").write_bytes(b"# log\n")
            (ws_dir / ".obsidian").mkdir(parents=True, exist_ok=True)
            (ws_dir / ".obsidian" / "workspace.json").write_bytes(b"{}")

            resp = await client.get(f"/v1/api/workspaces/{slug}/manifest")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            files = data["files"]
            assert "index.md" in files
            assert "concepts/foo.md" in files
            assert "raw/img.png" in files
            assert "logs/2026.md" not in files
            assert all(not k.startswith(".obsidian/") for k in files)
            assert all(not k.startswith(".keenyspace/") for k in files)
            for value in files.values():
                assert value.startswith("sha256:")
                assert len(value) == len("sha256:") + 64
            assert isinstance(data["server_canon_at"], str)


async def test_manifest_hash_byte_exact(app, pg_url) -> None:
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
            payload = b"# fixed bytes\ncontent line\n"
            target = ws_dir / "fixed.md"
            target.write_bytes(payload)
            expected = "sha256:" + hashlib.sha256(payload).hexdigest()

            resp = await client.get(f"/v1/api/workspaces/{slug}/manifest")
            assert resp.status_code == 200
            assert resp.json()["files"]["fixed.md"] == expected


async def test_manifest_invalid_slug_400(app, pg_url) -> None:
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
            resp = await client.get("/v1/api/workspaces/..etc/manifest")
            assert resp.status_code == 400
            assert resp.json()["detail"] == {"error": "invalid_slug"}


async def test_manifest_missing_workspace_404(app, pg_url) -> None:
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
            resp = await client.get("/v1/api/workspaces/never-existed/manifest")
            assert resp.status_code == 404


async def test_manifest_anonymous_401(app, pg_url) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            resp = await client.get("/v1/api/workspaces/any/manifest")
            assert resp.status_code == 401


async def test_pages_raw_returns_bytes(app, pg_url) -> None:
    """GET /pages-raw/{path} returns raw file bytes (added in Plan 05-03 Task 3)."""
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
            payload = b"# raw bytes\nline 2\n"
            (ws_dir / "concepts").mkdir(parents=True, exist_ok=True)
            (ws_dir / "concepts" / "foo.md").write_bytes(payload)

            resp = await client.get(f"/v1/api/workspaces/{slug}/pages-raw/concepts/foo.md")
            assert resp.status_code == 200
            assert resp.content == payload
            assert resp.headers["content-type"].startswith("application/octet-stream")


async def test_pages_raw_rejects_dotfiles(app, pg_url) -> None:
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
            for forbidden in (
                ".obsidian/workspace.json",
                ".keenyspace/config.yaml",
                "logs/2026.md",
                "tmp/junk.md",
                "../etc/passwd",
                "notes.txt",
            ):
                resp = await client.get(f"/v1/api/workspaces/{slug}/pages-raw/{forbidden}")
                assert resp.status_code in (400, 404), (
                    f"{forbidden!r} should be rejected, got {resp.status_code}"
                )


async def test_pages_raw_streams_binary_and_guards_paths(app, pg_url, tmp_path) -> None:
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
            payload = os.urandom(300 * 1024)
            (ws_dir / "raw").mkdir(parents=True, exist_ok=True)
            (ws_dir / "raw" / "blob.bin").write_bytes(payload)
            outside = tmp_path / "secret.md"
            outside.write_text("secret\n")
            (ws_dir / "raw" / "escape.md").symlink_to(outside)

            resp = await client.get(f"/v1/api/workspaces/{slug}/pages-raw/raw/blob.bin")
            assert resp.status_code == 200
            assert resp.content == payload
            assert resp.headers["content-type"].startswith("application/octet-stream")

            missing = await client.get(f"/v1/api/workspaces/{slug}/pages-raw/raw/nope.bin")
            assert missing.status_code == 404

            escaped = await client.get(f"/v1/api/workspaces/{slug}/pages-raw/raw/escape.md")
            assert escaped.status_code == 400
