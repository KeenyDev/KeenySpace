from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from keenyspace_server.wal.parser import parse_wal
from sqlalchemy import text

from tests.conftest import _reset_schema

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("KEENYSPACE_DB__URL"),
        reason="postgres unavailable; KEENYSPACE_DB__URL not set",
    ),
]


async def _seed_api_key() -> str:
    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session

    pepper = get_settings().auth.api_key_pepper.get_secret_value()
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    user_sub = f"logs-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :sub, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'logs', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": uuid4(),
                "sub": user_sub,
                "h": PasswordHasher().hash(body),
                "lh": hashlib.sha256(f"{body}{pepper}".encode()).hexdigest(),
                "now": now,
            },
        )
        await session.commit()
    return f"ks_live_{body}"


@contextlib.asynccontextmanager
async def _client(app, pg_url: str) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        api_key = await _seed_api_key()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as c:
            yield c


async def _create_workspace(client: AsyncClient) -> tuple[str, str]:
    slug = f"logs-{uuid4().hex[:8]}"
    resp = await client.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
    assert resp.status_code == 201, resp.text
    return slug, resp.json()["uuid"]


async def test_append_returns_written_entry_ts(app, pg_url: str, fs_root: Path) -> None:  # type: ignore[no-untyped-def]
    async with _client(app, pg_url) as client:
        slug, ws_uuid = await _create_workspace(client)

        resp = await client.post(f"/v1/api/workspaces/{slug}/logs", json={"workspace": slug, "content": "a fact"})

        assert resp.status_code == 201, resp.text
        body = resp.json()
        (log_file,) = (fs_root / "workspaces" / ws_uuid / "logs").glob("*.md")
        (entry,) = parse_wal(log_file.read_text())
        assert str(entry.id) == body["entry_id"]
        assert entry.ts == datetime.fromisoformat(body["ts"])


@pytest.mark.parametrize("content", ["", "   \n"])
async def test_empty_content_is_422(app, pg_url: str, content: str) -> None:  # type: ignore[no-untyped-def]
    async with _client(app, pg_url) as client:
        slug, _ = await _create_workspace(client)

        resp = await client.post(f"/v1/api/workspaces/{slug}/logs", json={"workspace": slug, "content": content})

        assert resp.status_code == 422, resp.text


async def test_malformed_parent_id_is_422(app, pg_url: str, fs_root: Path) -> None:  # type: ignore[no-untyped-def]
    async with _client(app, pg_url) as client:
        slug, ws_uuid = await _create_workspace(client)

        resp = await client.post(
            f"/v1/api/workspaces/{slug}/logs",
            json={"workspace": slug, "content": "a fact", "parent_id": "not-a-ulid"},
        )

        assert resp.status_code == 422, resp.text
        assert not list((fs_root / "workspaces" / ws_uuid / "logs").glob("*.md"))


async def test_oversized_content_is_413(app, pg_url: str) -> None:  # type: ignore[no-untyped-def]
    async with _client(app, pg_url) as client:
        slug, _ = await _create_workspace(client)

        resp = await client.post(
            f"/v1/api/workspaces/{slug}/logs",
            json={"workspace": slug, "content": "x" * (256 * 1024 + 1)},
        )

        assert resp.status_code == 413, resp.text


async def test_archived_workspace_is_409(app, pg_url: str) -> None:  # type: ignore[no-untyped-def]
    async with _client(app, pg_url) as client:
        slug, _ = await _create_workspace(client)
        archive = await client.post(f"/v1/api/workspaces/{slug}/archive")
        assert archive.status_code == 200, archive.text

        resp = await client.post(f"/v1/api/workspaces/{slug}/logs", json={"workspace": slug, "content": "a fact"})

        assert resp.status_code == 409, resp.text
        assert "archived" in resp.json()["detail"]
