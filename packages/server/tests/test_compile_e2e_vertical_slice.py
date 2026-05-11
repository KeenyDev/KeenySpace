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


async def _seed_workspace(client: AsyncClient, dev_token: str) -> tuple[str, Path]:
    slug = f"compile-vertical-{uuid4().hex[:8]}"
    resp = await client.post(
        "/v1/api/workspaces/",
        json={"slug": slug, "blueprint": "default"},
        headers={"Authorization": f"Bearer dev-{dev_token}"},
    )
    assert resp.status_code == 201, resp.text
    uuid_str = resp.json()["uuid"]
    ws_root = Path(os.environ["KEENYSPACE_FS__ROOT"]) / "workspaces" / uuid_str
    return slug, ws_root


async def _append_log(client: AsyncClient, slug: str, dev_token: str, content: str) -> None:
    resp = await client.post(
        f"/v1/api/workspaces/{slug}/logs",
        json={"content": content},
        headers={"Authorization": f"Bearer dev-{dev_token}"},
    )
    assert resp.status_code == 201, resp.text


async def test_post_compile_writes_a_page_end_to_end(tmp_path: Path) -> None:
    dev_token = "vertical-slice-token"
    os.environ["KEENYSPACE_AUTH__DEV_TOKEN"] = dev_token
    os.environ["KEENYSPACE_FS__ROOT"] = str(tmp_path)

    from keenyspace_server.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    from keenyspace_server.main import build_app
    app = build_app()

    from keenyspace_server.compile.agent import compile_agent
    from keenyspace_server.compile.models import CompilePlan, PageOp

    target_plan = CompilePlan(
        ops=[PageOp(
            action="create",
            path="notes/from-wal.md",
            body="Captured fragment: hello world\n",
            frontmatter={"title": "From WAL"},
        )],
    )

    async def _fake_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=target_plan.model_dump())])

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health_resp = await client.get("/healthz")
        if health_resp.status_code in (500, 503):
            pytest.skip("server not ready (engine_lifespan/migration not applied)")

        slug, ws_root = await _seed_workspace(client, dev_token)
        await _append_log(client, slug, dev_token, "hello world")

        with compile_agent.override(model=FunctionModel(_fake_model)):
            resp = await client.post(
                f"/v1/api/workspaces/{slug}/compile",
                headers={"Authorization": f"Bearer dev-{dev_token}"},
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
                _ws = (await _s.execute(sa_select2(WsModel).where(WsModel.slug == slug))).scalar_one()
            await coordinator.wait_for_idle(_ws.uuid, timeout=10.0)

        page = ws_root / "notes" / "from-wal.md"
        assert page.is_file(), f"Expected page at {page}; got dir contents: {list(ws_root.rglob('*'))}"
        text = page.read_text(encoding="utf-8")
        assert "Captured fragment: hello world" in text
        assert "title: From WAL" in text

        from keenyspace_server.db.models import CompileRun, Workspace
        from keenyspace_server.db.session import get_db_session
        from sqlalchemy import select as sa_select
        async with get_db_session() as session:
            ws = (await session.execute(sa_select(Workspace).where(Workspace.slug == slug))).scalar_one()
            runs = (await session.execute(
                sa_select(CompileRun).where(CompileRun.workspace_uuid == ws.uuid)
            )).scalars().all()
            assert len(runs) >= 1
            assert any(r.status == "success" and r.pages_written == 1 for r in runs)
