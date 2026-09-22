"""Phase 4 export->import roundtrip + import error-path integration tests
(WS-06 / D-07 / D-08).

Full lifespan + real Postgres; uses ASGITransport with API-key Bearer auth.
"""
from __future__ import annotations

import io
import os
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
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
    import hashlib
    import secrets

    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session

    pepper = get_settings().auth.api_key_pepper.get_secret_value()
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"roundtrip-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "rt", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'rt', "
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


async def _seed_workspace(client: AsyncClient) -> str:
    slug = f"src-{uuid4().hex[:8]}"
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"}
    )
    assert resp.status_code == 201, resp.text
    return slug


def _make_zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


async def test_export_import_roundtrip_preserves_pages(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    """Source-of-truth test for ROADMAP Phase 4 success criterion #4."""
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.models import Workspace
    from keenyspace_server.db.session import get_db_session

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

            slug_a = await _seed_workspace(client)
            async with get_db_session() as session:
                ws_a = (
                    await session.execute(
                        select(Workspace).where(Workspace.slug == slug_a)
                    )
                ).scalar_one()
            settings = get_settings()
            ws_a_dir = settings.fs.root / "workspaces" / str(ws_a.uuid)
            sample = ws_a_dir / "sample.md"
            sample_body = b"---\ntitle: Sample\n---\n# hello\n"
            sample.write_bytes(sample_body)

            exp = await client.get(f"/v1/api/workspaces/{slug_a}/export")
            assert exp.status_code == 200, exp.text
            zip_bytes = exp.content
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                assert "sample.md" in zf.namelist()

            slug_b = f"dst-{uuid4().hex[:8]}"
            imp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": slug_b},
                files={"file": ("a.zip", zip_bytes, "application/zip")},
            )
            assert imp.status_code == 201, imp.text
            payload = imp.json()
            assert payload["slug"] == slug_b
            assert payload["uuid"] != str(ws_a.uuid)

            async with get_db_session() as session:
                ws_b = (
                    await session.execute(
                        select(Workspace).where(Workspace.slug == slug_b)
                    )
                ).scalar_one()
            ws_b_dir = settings.fs.root / "workspaces" / str(ws_b.uuid)
            assert (ws_b_dir / "sample.md").read_bytes() == sample_body

            from keenyspace_server.db.models import AuditLog

            async with get_db_session() as session:
                actions = {
                    r.action
                    for r in (
                        await session.execute(select(AuditLog))
                    ).scalars().all()
                }
            assert "workspace.exported" in actions
            assert "workspace.imported" in actions


async def test_import_path_traversal_returns_422(app, pg_url) -> None:  # type: ignore[no-untyped-def]
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

            zb = _make_zip_bytes(
                [("../../../etc/passwd", b"x"), ("index.md", b"# x")]
            )
            resp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": "trav"},
                files={"file": ("evil.zip", zb, "application/zip")},
            )
            assert resp.status_code == 422
            assert resp.json()["detail"]["code"] == "path_traversal"


async def test_import_no_md_returns_422(app, pg_url) -> None:  # type: ignore[no-untyped-def]
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

            zb = _make_zip_bytes([("raw/img.png", b"\x89PNGdata")])
            resp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": "empty"},
                files={"file": ("empty.zip", zb, "application/zip")},
            )
            assert resp.status_code == 422
            assert resp.json()["detail"]["code"] == "empty_workspace"


async def test_import_bad_zip_returns_422(app, pg_url) -> None:  # type: ignore[no-untyped-def]
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

            resp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": "bogus"},
                files={
                    "file": ("bad.zip", b"not a zip", "application/zip")
                },
            )
            assert resp.status_code == 422
            assert resp.json()["detail"]["code"] == "bad_zip"


async def test_import_slug_conflict_returns_409(app, pg_url) -> None:  # type: ignore[no-untyped-def]
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

            existing = await _seed_workspace(client)
            zb = _make_zip_bytes([("index.md", b"# x")])
            resp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": existing},
                files={"file": ("a.zip", zb, "application/zip")},
            )
            assert resp.status_code == 409
            assert resp.json()["detail"]["code"] == "workspace_slug_conflict"


async def test_import_assigns_new_uuid_ignoring_source(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    from keenyspace_server.db.models import Workspace
    from keenyspace_server.db.session import get_db_session

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

            source_uuid = "11111111-1111-1111-1111-111111111111"
            cfg = (
                f"uuid: {source_uuid}\n"
                "slug: original\n"
                "blueprint: custom-bp@v0.2\n"
            ).encode()
            zb = _make_zip_bytes(
                [(".keenyspace/config.yaml", cfg), ("index.md", b"# x")]
            )
            slug = f"new-{uuid4().hex[:8]}"
            resp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": slug},
                files={"file": ("a.zip", zb, "application/zip")},
            )
            assert resp.status_code == 201, resp.text
            payload = resp.json()
            assert payload["uuid"] != source_uuid
            assert payload["slug"] == slug

            async with get_db_session() as session:
                ws = (
                    await session.execute(
                        select(Workspace).where(Workspace.slug == slug)
                    )
                ).scalar_one()
            assert ws.blueprint_ref == "custom-bp@v0.2"


async def test_import_unauthenticated_401(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")

            zb = _make_zip_bytes([("index.md", b"# x")])
            resp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": "anon"},
                files={"file": ("a.zip", zb, "application/zip")},
            )
            assert resp.status_code == 401


async def test_unmodified_default_blueprint_roundtrip_no_skip(app, pg_url) -> None:  # type: ignore[no-untyped-def]
    """G-4 regression: the canonical default-blueprint workspace roundtrips.

    Reproduces UAT Test 13. Before the G-4 fix, this test returns 422
    hidden_entry on raw/.gitkeep. After the fix, it returns 201 and the
    nested dotfile survives.

    Uses pytest.fail on non-200 health (NOT pytest.skip) -- the original UAT
    bug was hidden by tests that skipped past 5xx responses.
    """
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.models import AuditLog, Workspace
    from keenyspace_server.db.session import get_db_session

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
            if health.status_code != 200:
                pytest.fail(
                    f"/healthz not green: status={health.status_code} "
                    f"body={health.text}"
                )

            slug_a = await _seed_workspace(client)

            async with get_db_session() as session:
                ws_a = (
                    await session.execute(
                        select(Workspace).where(Workspace.slug == slug_a)
                    )
                ).scalar_one()
            settings = get_settings()
            ws_a_dir = settings.fs.root / "workspaces" / str(ws_a.uuid)
            assert (ws_a_dir / "raw" / ".gitkeep").exists(), (
                "default blueprint clone did not produce raw/.gitkeep -- "
                "G-4 test prerequisite missing; investigate clone_default_blueprint"
            )

            exp = await client.get(f"/v1/api/workspaces/{slug_a}/export")
            assert exp.status_code == 200, exp.text
            zip_bytes = exp.content
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                names = set(zf.namelist())
                assert "raw/.gitkeep" in names, (
                    "export did not include raw/.gitkeep -- export side may "
                    f"have regressed; namelist={sorted(names)}"
                )

            slug_b = f"g4-dst-{uuid4().hex[:8]}"
            imp = await client.post(
                "/v1/api/workspaces/import",
                data={"slug": slug_b},
                files={"file": ("a.zip", zip_bytes, "application/zip")},
            )
            assert imp.status_code == 201, (
                f"G-4 regression: import returned {imp.status_code} "
                f"body={imp.text}. Expected 201. The canonical default-"
                f"blueprint zip contains raw/.gitkeep which was rejected by "
                f"the pre-G-4 blanket hidden_entry rule."
            )

            payload = imp.json()
            async with get_db_session() as session:
                ws_b = (
                    await session.execute(
                        select(Workspace).where(Workspace.slug == payload["slug"])
                    )
                ).scalar_one()
            ws_b_dir = settings.fs.root / "workspaces" / str(ws_b.uuid)
            assert (ws_b_dir / "raw" / ".gitkeep").exists(), (
                "raw/.gitkeep was accepted by validator but missing on disk "
                "after import -- investigate _unpack_zip_sync"
            )

            async with get_db_session() as session:
                actions = {
                    r.action
                    for r in (
                        await session.execute(select(AuditLog))
                    ).scalars().all()
                }
            assert "workspace.exported" in actions
            assert "workspace.imported" in actions


async def test_import_rejects_oversized_content_length_before_reading_body(  # type: ignore[no-untyped-def]
    app, pg_url, monkeypatch
) -> None:
    from starlette.requests import Request

    async def _form_must_not_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("multipart body parsed despite oversized Content-Length")

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

            from keenyspace_server.api import workspace_import

            declared = (
                workspace_import._MAX_COMPRESSED_UPLOAD_BYTES
                + workspace_import._MULTIPART_OVERHEAD_BYTES
                + 1
            )
            monkeypatch.setattr(Request, "form", _form_must_not_run)
            resp = await client.post(
                "/v1/api/workspaces/import",
                content=b"--x--\r\n",
                headers={
                    "content-type": "multipart/form-data; boundary=x",
                    "content-length": str(declared),
                },
            )
            assert resp.status_code == 413, resp.text
            assert resp.json()["detail"]["code"] == "upload_too_large"
