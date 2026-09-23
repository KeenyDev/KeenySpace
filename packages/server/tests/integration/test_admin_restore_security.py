"""POST /v1/admin/restore security tests — Python 3.14 tarfile data filter.

Covers T-05.07-01 (path traversal), T-05.07-02 (absolute path), T-05.07-03
(symlink), missing manifest, and the anonymous 401 invariant.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

PG_URL = os.environ.get("KEENYSPACE_DB__URL")
HAS_PG_DUMP = shutil.which("pg_dump") is not None and shutil.which("psql") is not None

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
    pytest.mark.skipif(not HAS_PG_DUMP, reason="pg_dump/psql binary unavailable"),
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
    user_sub = f"sec-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at, "
                "groups, groups_seen_at) VALUES (:sub, :dn, NULL, 'api_key', :now, "
                "CAST(:groups AS jsonb), :now)"
            ),
            {"groups": '["keenyspace-admins"]', "sub": user_sub, "dn": "sec", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, "
                "lookup_hash, created_at) VALUES (:id, :sub, 'sec', "
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


async def _current_alembic_head() -> str:
    from keenyspace_server.db.session import get_db_session

    async with get_db_session() as session:
        row = await session.execute(text("SELECT version_num FROM alembic_version"))
        return row.scalar_one()


def _minimal_manifest(head: str) -> dict[str, Any]:
    return {
        "version": 1,
        "keenyspace_version": "0.1.0",
        "schema_version": 1,
        "alembic_head": head,
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": "test",
        "fs_root_size_bytes": 0,
        "workspaces": {"count": 0, "uuids": []},
        "blueprints": {"count": 0, "names": []},
        "pg_tables_dumped": ["alembic_version"],
    }


def _make_traversal_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        bad_info = tarfile.TarInfo(name="../etc/passwd")
        payload = b"root::0:0::/root:/bin/sh\n"
        bad_info.size = len(payload)
        tar.addfile(bad_info, io.BytesIO(payload))
    return buf.getvalue()


def _make_absolute_path_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="/etc/passwd")
        payload = b"x"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _make_symlink_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    return buf.getvalue()


def _make_tarball_without_manifest() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = b"SELECT 1;"
        info = tarfile.TarInfo(name="pg_dump.sql")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        di = tarfile.TarInfo(name="fs_root/workspaces")
        di.type = tarfile.DIRTYPE
        di.mode = 0o755
        tar.addfile(di)
    return buf.getvalue()


async def test_restore_rejects_path_traversal(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        tarball = _make_traversal_tarball()
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
                "/v1/admin/restore",
                files={"file": ("bad.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            # Python 3.14 data filter routes traversal through OutsideDestinationError
            # OR AbsolutePathError depending on the offending entry; both are
            # acceptable: the invariant is "rejected with a tampering code".
            assert detail["error"] in ("path_traversal", "absolute_path"), detail


async def test_restore_rejects_absolute_path(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    # Snapshot /etc/passwd before to guarantee the data filter did NOT write
    # to it regardless of how the attack surfaces as an error label.
    passwd_path = Path("/etc/passwd")
    passwd_size_before = passwd_path.stat().st_size if passwd_path.exists() else -1
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        tarball = _make_absolute_path_tarball()
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
                "/v1/admin/restore",
                files={"file": ("bad.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            # Python 3.14 data filter strips leading '/' from absolute paths
            # (so the entry CANNOT escape tmp_dir) rather than raising
            # AbsolutePathError. The resulting tarball lacks manifest.json
            # and surfaces as missing_manifest — equivalent guarantee: the
            # adversary cannot write /etc/passwd. Accept either label.
            assert detail["error"] in (
                "absolute_path",
                "path_traversal",
                "missing_manifest",
            ), detail
    # Absolute invariant: /etc/passwd MUST be untouched.
    passwd_size_after = passwd_path.stat().st_size if passwd_path.exists() else -1
    assert passwd_size_before == passwd_size_after


async def test_restore_rejects_symlinks(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        tarball = _make_symlink_tarball()
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
                "/v1/admin/restore",
                files={"file": ("bad.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            # Data filter rejects symlinks; outcome label may be 'symlink' or
            # 'malformed_tar' depending on the precise tarfile internal path.
            assert detail["error"] in ("symlink", "malformed_tar"), detail


async def test_restore_rejects_missing_manifest(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        _, plaintext = await _seed_api_key_post_lifespan()
        tarball = _make_tarball_without_manifest()
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
                "/v1/admin/restore",
                files={"file": ("bad.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            assert detail["error"] == "missing_manifest", detail


async def test_restore_anonymous_401(app: Any, pg_url: str) -> None:
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        # minimal valid-shaped tarball; anonymous request must fail at auth
        # gate before reaching extraction.
        head = "0001"
        manifest = json.dumps(_minimal_manifest(head)).encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
        tarball = buf.getvalue()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            if health.status_code in (500, 503):
                pytest.skip("server not ready")
            resp = await client.post(
                "/v1/admin/restore",
                files={"file": ("bad.tar.gz", tarball, "application/gzip")},
            )
            assert resp.status_code == 401
