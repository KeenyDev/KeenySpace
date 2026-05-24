"""Phase 5 workspace manifest endpoint integration tests (Plan 05-03 Task 1).

Endpoint: GET /v1/api/workspaces/<slug>/manifest -> {files: {path: sha256:<hex>}, server_canon_at}.

Per D-13: manifest scope = .md anywhere + raw/ subtree only; .obsidian / .keenyspace /
logs / tmp top-level dirs MUST be excluded.
"""
from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"
    ),
]


async def _reset_schema(pg_url: str) -> None:
    import sqlalchemy as sa

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


async def _seed_api_key_post_lifespan() -> tuple[str, str]:
    import base64
    import hashlib as _h
    import secrets

    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session

    pepper = get_settings().auth.api_key_pepper
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = _h.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"manifest-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "manifest", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'manifest', "
                "'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
        await session.commit()

    return user_sub, f"ks_live_{body}"


async def _seed_workspace(client: AsyncClient, slug: str | None = None) -> str:
    slug = slug or f"mf-{uuid4().hex[:8]}"
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"}
    )
    assert resp.status_code == 201, resp.text
    return slug


def _workspace_dir(app, slug: str) -> Path:
    from keenyspace_server.db.models import Workspace as _Workspace  # noqa: F401

    # Walk fs_root/workspaces, picking the dir whose .keenyspace/config.yaml mentions slug.
    fs_root = Path(app.state.settings.fs.root) / "workspaces"
    for entry in fs_root.iterdir():
        cfg = entry / ".keenyspace" / "config.yaml"
        if cfg.is_file() and f"slug: {slug}" in cfg.read_text():
            return entry
    raise AssertionError(f"workspace dir for {slug!r} not found under {fs_root}")


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

            resp = await client.get(
                f"/v1/api/workspaces/{slug}/pages-raw/concepts/foo.md"
            )
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
                resp = await client.get(
                    f"/v1/api/workspaces/{slug}/pages-raw/{forbidden}"
                )
                assert resp.status_code in (400, 404), (
                    f"{forbidden!r} should be rejected, got {resp.status_code}"
                )
