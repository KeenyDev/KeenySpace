from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
import yaml
from httpx import ASGITransport, AsyncClient

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"
    ),
]


async def _reset_schema(pg_url: str) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


async def _seed_api_key_post_lifespan() -> tuple[str, str]:
    import base64
    import hashlib
    import secrets
    from uuid import uuid4 as _uuid4

    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session
    from sqlalchemy import text

    pepper = get_settings().auth.api_key_pepper
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"catalog-{_uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "catalog", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'catalog', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": _uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
        await session.commit()

    return user_sub, f"ks_live_{body}"


def _install_fixture_blueprint(
    fs_root: Path,
    name: str,
    blueprint_yaml_data: dict,
    extras: dict[str, str] | None = None,
) -> None:
    bp_dir = fs_root / "blueprints" / name
    (bp_dir / ".keenyspace").mkdir(parents=True, exist_ok=True)
    (bp_dir / ".keenyspace" / "blueprint.yaml").write_text(
        yaml.safe_dump(blueprint_yaml_data)
    )
    for relpath, content in (extras or {}).items():
        target = bp_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def _install_raw_blueprint_yaml(fs_root: Path, name: str, raw_yaml: str) -> None:
    bp_dir = fs_root / "blueprints" / name
    (bp_dir / ".keenyspace").mkdir(parents=True, exist_ok=True)
    (bp_dir / ".keenyspace" / "blueprint.yaml").write_text(raw_yaml)


async def _seed_workspace(
    client: AsyncClient, slug: str, blueprint: str = "default"
) -> tuple[str, str]:
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": slug, "blueprint": blueprint}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return slug, body["uuid"]


def _make_fastmcp_client(app, plaintext: str):
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        follow_redirects: bool = True,
        **kwargs: object,
    ) -> httpx.AsyncClient:
        merged_headers: dict[str, str] = {
            "Authorization": f"Bearer {plaintext}",
        }
        if headers:
            merged_headers.update(headers)
        return httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers=merged_headers,
            timeout=timeout or httpx.Timeout(30.0, read=60.0),
            follow_redirects=follow_redirects,
        )

    transport = StreamableHttpTransport(
        url="http://test/v1/mcp/",
        httpx_client_factory=factory,
    )
    return Client(transport)


async def test_list_blueprints_discovers_default(app, pg_url) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test",
                               headers={"Authorization": f"Bearer {plaintext}"}) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
        async with _make_fastmcp_client(app, plaintext) as mcp_client:
            resp = await mcp_client.call_tool("list_blueprints", {})
        data = resp.structured_content if hasattr(resp, "structured_content") else resp.data
        names = {b["name"] for b in data["blueprints"]}
        assert "default" in names


async def test_list_blueprints_discovers_fixture_blueprint(app, pg_url) -> None:
    await _reset_schema(pg_url)
    fs_root = app.state.settings.fs.root
    _install_fixture_blueprint(
        fs_root,
        "test-bp",
        {"version": "v0.1", "description": "test fixture"},
        extras={"CLAUDE.md": "# test-bp\n"},
    )
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test",
                               headers={"Authorization": f"Bearer {plaintext}"}) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
        async with _make_fastmcp_client(app, plaintext) as mcp_client:
            resp = await mcp_client.call_tool("list_blueprints", {})
        data = resp.structured_content if hasattr(resp, "structured_content") else resp.data
        blueprints = data["blueprints"]
        names = {b["name"] for b in blueprints}
        assert "default" in names
        assert "test-bp" in names
        test_bp = next(b for b in blueprints if b["name"] == "test-bp")
        assert test_bp["description"] == "test fixture"


async def test_clone_test_bp_creates_workspace(app, pg_url) -> None:
    await _reset_schema(pg_url)
    fs_root = app.state.settings.fs.root
    _install_fixture_blueprint(
        fs_root,
        "test-bp",
        {"version": "v0.1", "description": "test fixture"},
        extras={"CLAUDE.md": "# test-bp\n", "index.md": "# index\n"},
    )
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
            _, ws_uuid = await _seed_workspace(client, "test-ws", blueprint="test-bp")
        ws_dir = fs_root / "workspaces" / ws_uuid
        assert (ws_dir / "CLAUDE.md").is_file()
        cfg = yaml.safe_load((ws_dir / ".keenyspace" / "config.yaml").read_text())
        assert cfg.get("blueprint") == "test-bp@v0.1" or cfg.get("blueprint_ref") == "test-bp@v0.1"


async def test_clone_default_moves_instructions_to_keenyspace_dir(app, pg_url) -> None:
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
            fs_root = app.state.settings.fs.root
            _, ws_uuid = await _seed_workspace(client, "ingest-ws", blueprint="default")
    ws_dir = fs_root / "workspaces" / ws_uuid
    assert (ws_dir / ".keenyspace" / "instructions" / "ingest.md").is_file()
    assert not (ws_dir / "_instructions").exists()


async def test_get_instructions_renders_with_context(app, pg_url) -> None:
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
            await _seed_workspace(client, "ingest-ws", blueprint="default")
        async with _make_fastmcp_client(app, plaintext) as mcp_client:
            resp = await mcp_client.call_tool(
                "get_instructions",
                {
                    "workspace": "ingest-ws",
                    "command": "ingest",
                    "context": {"source_path": "/data/src.md"},
                },
            )
    data = resp.structured_content if hasattr(resp, "structured_content") else resp.data
    assert "/data/src.md" in data["prompt"]
    assert "ingest-ws" in data["prompt"]
    assert any("/data/src.md" in step for step in data["steps"])
    assert data["tool_whitelist"] == ["read_page", "append_log", "search_workspace"]
    assert data["model"] is None


async def test_get_instructions_strict_undefined_raises(app, pg_url) -> None:
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
            await _seed_workspace(client, "ingest-ws", blueprint="default")
        async with _make_fastmcp_client(app, plaintext) as mcp_client:
            with pytest.raises(Exception, match="instructions_template_error"):
                await mcp_client.call_tool(
                    "get_instructions",
                    {
                        "workspace": "ingest-ws",
                        "command": "ingest",
                        "context": {},
                    },
                )


async def test_get_instructions_dunder_blocked_raises(app, pg_url) -> None:
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
            fs_root = app.state.settings.fs.root
            _, ws_uuid = await _seed_workspace(client, "sandbox-ws", blueprint="default")
        sandbox_path = (
            fs_root
            / "workspaces"
            / ws_uuid
            / ".keenyspace"
            / "instructions"
            / "sandbox-test.md"
        )
        sandbox_path.parent.mkdir(parents=True, exist_ok=True)
        sandbox_path.write_text(
            "---\n"
            "tool_whitelist: []\n"
            "steps: []\n"
            "budgets:\n"
            "  max_steps: 10\n"
            "  max_tokens: 10000\n"
            "  max_seconds: 60\n"
            "---\n"
            "{{ workspace.__class__ }}\n"
        )
        async with _make_fastmcp_client(app, plaintext) as mcp_client:
            with pytest.raises(Exception, match="instructions_template_error"):
                await mcp_client.call_tool(
                    "get_instructions",
                    {
                        "workspace": "sandbox-ws",
                        "command": "sandbox-test",
                        "context": {},
                    },
                )


async def test_list_blueprints_skips_malformed_yaml(app, pg_url) -> None:
    await _reset_schema(pg_url)
    fs_root = app.state.settings.fs.root
    _install_raw_blueprint_yaml(fs_root, "bad-bp", "{not: valid yaml: [\n")
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test",
                               headers={"Authorization": f"Bearer {plaintext}"}) as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
        with structlog.testing.capture_logs() as captured:
            async with _make_fastmcp_client(app, plaintext) as mcp_client:
                resp = await mcp_client.call_tool("list_blueprints", {})
    data = resp.structured_content if hasattr(resp, "structured_content") else resp.data
    names = {b["name"] for b in data["blueprints"]}
    assert "bad-bp" not in names
    assert any(
        event.get("event") == "blueprint.yaml_parse_failed" for event in captured
    )
