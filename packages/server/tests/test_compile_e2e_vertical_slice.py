from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
]


async def _seed_workspace(client: AsyncClient, fs_root: Path) -> tuple[str, Path]:
    slug = f"compile-vertical-{uuid4().hex[:8]}"
    resp = await client.post(
        "/v1/api/workspaces/",
        json={"slug": slug, "blueprint": "default"},
    )
    assert resp.status_code == 201, resp.text
    uuid_str = resp.json()["uuid"]
    ws_root = fs_root / "workspaces" / uuid_str
    return slug, ws_root


async def _append_log(client: AsyncClient, slug: str, content: str) -> None:
    resp = await client.post(
        f"/v1/api/workspaces/{slug}/logs",
        json={"workspace": slug, "content": content},
    )
    assert resp.status_code == 201, resp.text


async def test_post_compile_writes_a_page_end_to_end(app, fs_root: Path, pg_url) -> None:
    """E2E через настоящую auth chain: Bearer ks_live_* -> CompositeAuthBackend -> route."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    # Reset schema (mirrors _engine_lifespan_ctx prep) so app_lifespan starts clean.
    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()

    from keenyspace_server.compile.agent import compile_agent
    from keenyspace_server.compile.models import CompilePlan, PageOp

    target_plan = CompilePlan(
        ops=[
            PageOp(
                action="create",
                path="notes/from-wal.md",
                body="Captured fragment: hello world\n",
                frontmatter={"title": "From WAL"},
            )
        ],
    )

    async def _fake_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool, args=target_plan.model_dump())]
        )

    async with app.router.lifespan_context(app):
        # Seed api_key after migration ran (api_keys table now exists).
        _, plaintext = await _seed_api_key_post_lifespan()

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            health_resp = await client.get("/healthz")
            if health_resp.status_code in (500, 503):
                pytest.skip("server not ready (engine_lifespan/migration not applied)")

            slug, ws_root = await _seed_workspace(client, fs_root)
            await _append_log(client, slug, "hello world")

            with compile_agent.override(model=FunctionModel(_fake_model)):
                resp = await client.post(
                    f"/v1/api/workspaces/{slug}/compile",
                )

            assert resp.status_code == 202, resp.text
            body = resp.json()
            assert body["status"] in ("queued", "running", "idempotent_noop")
            assert isinstance(body["job_id"], str) and len(body["job_id"]) > 0

            coordinator = app.state.compile_coordinator
            if coordinator is not None:
                from keenyspace_server.db.models import Workspace as WsModel
                from keenyspace_server.db.session import get_db_session as _get_db
                from sqlalchemy import select as sa_select2

                async with _get_db() as _s:
                    _ws = (
                        await _s.execute(sa_select2(WsModel).where(WsModel.slug == slug))
                    ).scalar_one()
                await coordinator.wait_for_idle(_ws.uuid, timeout=10.0)

            page = ws_root / "notes" / "from-wal.md"
            assert page.is_file(), (
                f"Expected page at {page}; got dir contents: {list(ws_root.rglob('*'))}"
            )
            text = page.read_text(encoding="utf-8")
            assert "Captured fragment: hello world" in text
            assert "title: From WAL" in text

            from keenyspace_server.db.models import CompileRun, Workspace
            from keenyspace_server.db.session import get_db_session
            from sqlalchemy import select as sa_select

            async with get_db_session() as session:
                ws = (
                    await session.execute(sa_select(Workspace).where(Workspace.slug == slug))
                ).scalar_one()
                runs = (
                    (
                        await session.execute(
                            sa_select(CompileRun).where(CompileRun.workspace_uuid == ws.uuid)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(runs) >= 1
                assert any(r.status == "success" and r.pages_written == 1 for r in runs)


async def _seed_api_key_post_lifespan() -> tuple[str, str]:
    """Seed user + api_key after app_lifespan has migrated schema. Returns (user_sub, plaintext)."""
    import base64
    import hashlib
    import secrets
    from datetime import UTC, datetime
    from uuid import uuid4

    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session
    from sqlalchemy import text

    pepper = get_settings().auth.api_key_pepper
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"e2e-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "e2e", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'e2e', 'ks_live_', :h, :lh, :now)"
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
