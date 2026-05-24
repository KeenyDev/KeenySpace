"""POST /v1/admin/backup + /v1/admin/restore endpoints (Phase 5 F-06).

Backup streams a gzipped tarball whose first entry is manifest.json (BackupManifest
shape from keenyspace_shared.mcp_contracts), followed by pg_dump.sql and the
fs_root/workspaces + fs_root/blueprints subtrees with `.obsidian` filtered out.

Restore extracts via Python 3.14's `tarfile.extractall(filter="data")` to refuse
path-traversal / absolute paths / symlinks, validates the manifest's
keenyspace_version + alembic_head against the running server, and refuses a
non-empty target unless `?force=true` is supplied (which then wipes via
FK-aware DELETE + rmtree before applying).

Pitfall #8 (atomic same-volume tmp) is the reason every pg_dump/restore scratch
directory lives under `fs_root/tmp/` rather than `/tmp` — `os.rename` between
volumes degrades to copy+delete and breaks the atomic FS move at step 8.
Pitfall #3 (Python 3.14 tarfile default filter) is set explicitly to `"data"`
so a future stdlib regression does not silently widen the attack surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import secrets
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import semver as _semver
import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from keenyspace_shared.mcp_contracts import BackupManifest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.auth.audit import write_audit
from keenyspace_server.db.session import get_db
from keenyspace_server.observability.metrics import (
    ADMIN_BACKUP_BYTES,
    ADMIN_BACKUP_TOTAL,
    ADMIN_RESTORE_TOTAL,
    ADMIN_RESTORE_WIPED_TOTAL,
)

log = structlog.get_logger(__name__)
router = APIRouter()

KS_VERSION = "0.1.0"

PG_TABLES_DUMPED = [
    "users",
    "workspaces",
    "api_keys",
    "audit_log",
    "blueprints",
    "compile_cursors",
    "compile_runs",
    "alembic_version",
]

PG_TABLES_FK_ORDER = [
    "audit_log",
    "api_keys",
    "compile_runs",
    "compile_cursors",
    "workspaces",
    "blueprints",
    "users",
    "alembic_version",
]

UPLOAD_CHUNK_BYTES = 65536


def _pg_dump_argv(db_url: str) -> list[str]:
    """Translate SQLAlchemy URL to libpq-style argv for pg_dump / psql.

    SQLAlchemy URLs use `postgresql+asyncpg://` and ship asyncpg-specific query
    parameters; pg_dump uses libpq, so strip the `+driver` and pass discrete
    flags. Password (if any) is forwarded via PGPASSWORD env in the caller.
    """
    parsed = urlparse(db_url)
    argv = [
        "pg_dump",
        "--no-owner",
        "--no-acl",
        # --clean --if-exists emits "DROP TABLE IF EXISTS ..." before every
        # CREATE so psql can replay against a target whose schema already
        # exists (Alembic ran during server boot). D-17 wipes ROWS via DELETE,
        # not tables — without --clean the replay collides on CREATE TABLE.
        "--clean",
        "--if-exists",
    ]
    for table in PG_TABLES_DUMPED:
        argv.append(f"--table={table}")
    if parsed.hostname:
        argv.extend(["-h", parsed.hostname])
    if parsed.port:
        argv.extend(["-p", str(parsed.port)])
    if parsed.username:
        argv.extend(["-U", parsed.username])
    dbname = parsed.path.lstrip("/") or "postgres"
    argv.append(dbname)
    return argv


def _psql_argv(db_url: str) -> list[str]:
    parsed = urlparse(db_url)
    argv = ["psql", "--single-transaction", "-v", "ON_ERROR_STOP=1"]
    if parsed.hostname:
        argv.extend(["-h", parsed.hostname])
    if parsed.port:
        argv.extend(["-p", str(parsed.port)])
    if parsed.username:
        argv.extend(["-U", parsed.username])
    dbname = parsed.path.lstrip("/") or "postgres"
    argv.extend(["-d", dbname])
    return argv


def _pg_env(db_url: str) -> dict[str, str]:
    parsed = urlparse(db_url)
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return env


async def _current_alembic_head(session: AsyncSession) -> str:
    row = await session.execute(text("SELECT version_num FROM alembic_version"))
    value = row.scalar_one_or_none()
    return value or "unknown"


@router.post("/backup")
async def admin_backup(
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    user = request.user
    settings = request.app.state.settings
    fs_root: Path = Path(settings.fs.root)

    ws_count_row = await session.execute(text("SELECT count(*) FROM workspaces"))
    ws_count = ws_count_row.scalar_one()
    await write_audit(
        session,
        actor_sub=user.sub,
        action="admin.backup.requested",
        payload={"workspace_count": int(ws_count)},
    )
    await session.commit()
    alembic_head = await _current_alembic_head(session)
    db_url = settings.db.url

    async def _stream() -> Any:
        tmp_dir = fs_root / "tmp" / f"backup-{secrets.token_hex(8)}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        pg_dump_path = tmp_dir / "pg_dump.sql"
        total_bytes = 0
        try:
            # WR-02: stream pg_dump stdout to disk in chunks instead of
            # buffering the whole dump into a single bytes object. A
            # multi-GB production database would otherwise pin O(dump_size)
            # RSS in the worker process and risk OOM kills mid-backup.
            proc = await asyncio.create_subprocess_exec(
                *_pg_dump_argv(db_url),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_pg_env(db_url),
            )
            assert proc.stdout is not None
            assert proc.stderr is not None
            with pg_dump_path.open("wb") as pg_fp:
                while True:
                    pg_chunk = await proc.stdout.read(UPLOAD_CHUNK_BYTES)
                    if not pg_chunk:
                        break
                    pg_fp.write(pg_chunk)
            pg_err = await proc.stderr.read()
            await proc.wait()
            if proc.returncode != 0:
                log.error(
                    "admin.backup.pg_dump_failed",
                    stderr=pg_err.decode(errors="replace"),
                )
                raise HTTPException(500, {"error": "pg_dump_failed"})

            workspaces_dir = fs_root / "workspaces"
            ws_uuids = (
                sorted(d.name for d in workspaces_dir.iterdir() if d.is_dir())
                if workspaces_dir.exists()
                else []
            )
            blueprints_dir = fs_root / "blueprints"
            bp_names = (
                sorted(d.name for d in blueprints_dir.iterdir() if d.is_dir())
                if blueprints_dir.exists()
                else []
            )
            fs_root_size = (
                sum(
                    p.stat().st_size
                    for p in workspaces_dir.rglob("*")
                    if p.is_file()
                )
                if workspaces_dir.exists()
                else 0
            )
            manifest = BackupManifest(
                version=1,
                keenyspace_version=KS_VERSION,
                schema_version=1,
                alembic_head=alembic_head,
                created_at=datetime.now(UTC),
                created_by=user.sub,
                fs_root_size_bytes=fs_root_size,
                workspaces={"count": len(ws_uuids), "uuids": ws_uuids},
                blueprints={"count": len(bp_names), "names": bp_names},
                pg_tables_dumped=list(PG_TABLES_DUMPED),
            )
            manifest_bytes = manifest.model_dump_json(indent=2).encode()

            def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                parts = info.name.split("/")
                if ".obsidian" in parts:
                    return None
                if info.issym() or info.islnk():
                    return None
                return info

            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w|gz") as tar:
                manifest_info = tarfile.TarInfo(name="manifest.json")
                manifest_info.size = len(manifest_bytes)
                manifest_info.mtime = int(datetime.now(UTC).timestamp())
                tar.addfile(manifest_info, io.BytesIO(manifest_bytes))
                chunk = buf.getvalue()
                if chunk:
                    total_bytes += len(chunk)
                    yield chunk
                buf.seek(0)
                buf.truncate()

                pg_info = tarfile.TarInfo(name="pg_dump.sql")
                pg_info.size = pg_dump_path.stat().st_size
                pg_info.mtime = int(datetime.now(UTC).timestamp())
                with pg_dump_path.open("rb") as fp:
                    tar.addfile(pg_info, fp)
                chunk = buf.getvalue()
                if chunk:
                    total_bytes += len(chunk)
                    yield chunk
                buf.seek(0)
                buf.truncate()

                # WR-07: tar.add() recursively walks the directory tree
                # synchronously, reading file bytes and writing them to the
                # tarfile object. Under single-worker uvicorn that blocks
                # every other handler — health probes, hook callbacks,
                # MCP tool calls — for the duration of the walk. Offload
                # to a worker thread; the BytesIO buf is drained AFTER the
                # to_thread call (no concurrent writer/reader, no need to
                # interleave yield mid-walk for correctness).
                def _tar_add_dir(
                    tar_obj: tarfile.TarFile, src: Path, arcname: str
                ) -> None:
                    tar_obj.add(str(src), arcname=arcname, filter=_filter)

                if workspaces_dir.exists():
                    await asyncio.to_thread(
                        _tar_add_dir, tar, workspaces_dir, "fs_root/workspaces"
                    )
                    chunk = buf.getvalue()
                    if chunk:
                        total_bytes += len(chunk)
                        yield chunk
                    buf.seek(0)
                    buf.truncate()

                if blueprints_dir.exists():
                    await asyncio.to_thread(
                        _tar_add_dir, tar, blueprints_dir, "fs_root/blueprints"
                    )
                    chunk = buf.getvalue()
                    if chunk:
                        total_bytes += len(chunk)
                        yield chunk
                    buf.seek(0)
                    buf.truncate()

            tail = buf.getvalue()
            if tail:
                total_bytes += len(tail)
                yield tail
            ADMIN_BACKUP_BYTES.inc(total_bytes)
            ADMIN_BACKUP_TOTAL.inc()
            log.info(
                "admin.backup.completed",
                user_sub=user.sub,
                total_bytes=total_bytes,
                workspace_count=len(ws_uuids),
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    iso = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="keenyspace-backup-{iso}.tar.gz"'
            ),
        },
    )


@router.post("/restore")
async def admin_restore(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    force: bool = Query(False),
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    user = request.user
    settings = request.app.state.settings
    fs_root: Path = Path(settings.fs.root)
    db_url = settings.db.url
    fs_root.mkdir(parents=True, exist_ok=True)
    tmp_parent = fs_root / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = tmp_parent / f"restore-{secrets.token_hex(8)}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_parent / f"{tmp_dir.name}.tar.gz"
    try:
        with archive_path.open("wb") as fp:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                fp.write(chunk)

        # WR-07: tar.extractall walks the entire archive synchronously
        # (file IO + writes). Offload to a worker thread so the event
        # loop can continue serving health probes and concurrent
        # requests during a large restore.
        def _extract_tar() -> None:
            with tarfile.open(archive_path, "r:gz") as tar:
                # Pitfall #3: Python 3.14 defaults tarfile filter to "data" but
                # we set it explicitly so a future stdlib regression cannot widen
                # the attack surface silently.
                tar.extractall(path=tmp_dir, filter="data")

        try:
            await asyncio.to_thread(_extract_tar)
        except tarfile.OutsideDestinationError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="path_traversal").inc()
            raise HTTPException(
                422, {"error": "path_traversal", "detail": str(exc)}
            ) from exc
        except tarfile.AbsolutePathError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="absolute_path").inc()
            raise HTTPException(
                422, {"error": "absolute_path", "detail": str(exc)}
            ) from exc
        except tarfile.LinkOutsideDestinationError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="symlink").inc()
            raise HTTPException(
                422, {"error": "symlink", "detail": str(exc)}
            ) from exc
        except tarfile.TarError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="malformed").inc()
            raise HTTPException(
                422, {"error": "malformed_tar", "detail": str(exc)}
            ) from exc

        manifest_path = tmp_dir / "manifest.json"
        if not manifest_path.exists():
            ADMIN_RESTORE_TOTAL.labels(outcome="missing_manifest").inc()
            raise HTTPException(422, {"error": "missing_manifest"})
        manifest = BackupManifest.model_validate_json(manifest_path.read_text())

        try:
            source = _semver.VersionInfo.parse(manifest.keenyspace_version)
            target = _semver.VersionInfo.parse(KS_VERSION)
        except ValueError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="bad_version").inc()
            raise HTTPException(
                422, {"error": "bad_version", "detail": str(exc)}
            ) from exc
        if (source.major, source.minor) != (target.major, target.minor) and not force:
            ADMIN_RESTORE_TOTAL.labels(outcome="version_mismatch").inc()
            raise HTTPException(
                422,
                {
                    "error": "version_mismatch",
                    "source": str(source),
                    "target": str(target),
                },
            )
        current_head = await _current_alembic_head(session)
        if manifest.alembic_head != current_head and not force:
            ADMIN_RESTORE_TOTAL.labels(outcome="schema_mismatch").inc()
            raise HTTPException(
                422,
                {
                    "error": "schema_mismatch",
                    "alembic_head_source": manifest.alembic_head,
                    "alembic_head_target": current_head,
                },
            )

        existing = (
            await session.execute(text("SELECT count(*) FROM workspaces"))
        ).scalar_one()
        existing = int(existing)
        ws_dir = fs_root / "workspaces"
        existing_dirs = (
            [d.name for d in ws_dir.iterdir() if d.is_dir()]
            if ws_dir.exists()
            else []
        )
        if (existing > 0 or existing_dirs) and not force:
            ADMIN_RESTORE_TOTAL.labels(outcome="target_not_empty").inc()
            raise HTTPException(
                409,
                {
                    "error": "target_not_empty",
                    "existing_workspaces": existing,
                    "existing_fs_uuids": existing_dirs,
                },
            )

        if force and (existing > 0 or existing_dirs):
            for table in PG_TABLES_FK_ORDER:
                await session.execute(text(f"DELETE FROM {table}"))
            await session.commit()
            shutil.rmtree(fs_root / "workspaces", ignore_errors=True)
            shutil.rmtree(fs_root / "blueprints", ignore_errors=True)
            ADMIN_RESTORE_WIPED_TOTAL.inc()
            await write_audit(
                session,
                actor_sub=user.sub,
                action="admin.restore.wipe",
                payload={
                    "target_workspace_count_before": existing,
                    "target_fs_uuid_count_before": len(existing_dirs),
                },
            )
            await session.commit()

        pg_dump_path = tmp_dir / "pg_dump.sql"
        if not pg_dump_path.exists():
            ADMIN_RESTORE_TOTAL.labels(outcome="missing_pg_dump").inc()
            raise HTTPException(422, {"error": "missing_pg_dump"})

        # WR-02: stream the extracted pg_dump file into psql's stdin in
        # chunks rather than loading the entire dump into a single bytes
        # buffer before piping. For a multi-GB dump the buffered variant
        # holds two copies (file bytes + subprocess input) in RSS.
        psql = await asyncio.create_subprocess_exec(
            *_psql_argv(db_url),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_pg_env(db_url),
        )
        assert psql.stdin is not None
        assert psql.stderr is not None
        with pg_dump_path.open("rb") as dump_fp:
            while True:
                dump_chunk = dump_fp.read(UPLOAD_CHUNK_BYTES)
                if not dump_chunk:
                    break
                psql.stdin.write(dump_chunk)
                await psql.stdin.drain()
        psql.stdin.close()
        psql_err = await psql.stderr.read()
        await psql.wait()
        if psql.returncode != 0:
            ADMIN_RESTORE_TOTAL.labels(outcome="psql_restore_failed").inc()
            log.error(
                "admin.restore.psql_failed",
                stderr=psql_err.decode(errors="replace"),
            )
            raise HTTPException(
                500,
                {
                    "error": "psql_restore_failed",
                    "detail": psql_err.decode(errors="replace")[:500],
                },
            )

        src_workspaces = tmp_dir / "fs_root" / "workspaces"
        src_blueprints = tmp_dir / "fs_root" / "blueprints"
        (fs_root / "workspaces").mkdir(parents=True, exist_ok=True)
        if src_workspaces.exists():
            for item in src_workspaces.iterdir():
                # Pitfall #8: same-volume rename so the move stays atomic; tmp
                # lives under fs_root by construction.
                os.rename(item, fs_root / "workspaces" / item.name)
        (fs_root / "blueprints").mkdir(parents=True, exist_ok=True)
        if src_blueprints.exists():
            for item in src_blueprints.iterdir():
                os.rename(item, fs_root / "blueprints" / item.name)

        await write_audit(
            session,
            actor_sub=user.sub,
            action="admin.restore.applied",
            payload={
                "source_version": manifest.keenyspace_version,
                "target_version": KS_VERSION,
                "wiped": force,
                "workspace_count_restored": int(manifest.workspaces.get("count", 0)),
            },
        )
        await session.commit()
        ADMIN_RESTORE_TOTAL.labels(outcome="success").inc()
        return {
            "ok": True,
            "workspaces_restored": int(manifest.workspaces.get("count", 0)),
            "wiped": force,
        }
    finally:
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
