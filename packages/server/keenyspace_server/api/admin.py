"""POST /v1/admin/backup + /v1/admin/restore endpoints.

Backup builds a gzipped tarball under ``fs_root/tmp/`` and then streams it. The
first entry is manifest.json (BackupManifest shape from
keenyspace_shared.mcp_contracts), followed by pg_dump.sql and the
fs_root/workspaces + fs_root/blueprints subtrees with `.obsidian` and links
filtered out.

Restore extracts via Python 3.14's `tarfile.extractall(filter="data")` and
refuses any link member, validates the manifest's keenyspace_version +
alembic_head against the running server, refuses a non-empty target unless
`?force=true` is supplied, and refuses a pg_dump.sql that psql could execute as
a client-side meta-command. Every check runs before anything is changed. The
restored trees are then swapped in with the replaced entries parked aside, the
dump is replayed in one psql transaction (with the force wipe prepended to that
same transaction), and the parked entries are deleted only once both steps
succeeded — any failure puts the old trees back and leaves the database as it
was.

Every pg_dump/restore scratch directory lives under `fs_root/tmp/` rather than
`/tmp`: `os.rename` between volumes degrades to copy+delete, which would break
the atomic FS swap. The tarfile extraction filter is passed explicitly rather
than relying on the stdlib default, so a future default change cannot silently
widen the attack surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import secrets
import shutil
import tarfile
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import semver as _semver
import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from keenyspace_shared.mcp_contracts import BackupManifest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.auth.admin_gate import AdminRoute
from keenyspace_server.auth.api_keys import ApiKeyService
from keenyspace_server.auth.audit import write_audit
from keenyspace_server.auth.group_snapshot import GroupSnapshotStore
from keenyspace_server.auth.schemas import ApiKeyRevokeAllRequest, ApiKeyRevokeAllResponse
from keenyspace_server.db.session import get_db, get_engine
from keenyspace_server.observability.metrics import (
    ADMIN_BACKUP_BYTES,
    ADMIN_BACKUP_TOTAL,
    ADMIN_RESTORE_TOTAL,
    ADMIN_RESTORE_WIPED_TOTAL,
)

log = structlog.get_logger(__name__)
router = APIRouter(route_class=AdminRoute)

# Written into every backup manifest and compared (major.minor) on restore, so it
# must stay semver-parseable — not the PEP 440 spelling used in pyproject.
KS_VERSION = "0.2.0-alpha.1"

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

FS_TREES = ("workspaces", "blueprints")

UPLOAD_CHUNK_BYTES = 65536
PSQL_LOCK_TIMEOUT_MS = 30_000
PG_CLIENT_TIMEOUT_S = 30 * 60


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
        # exists (Alembic ran during server boot). The force wipe removes ROWS
        # via DELETE, not tables — without --clean the replay would collide on
        # CREATE TABLE.
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
        argv.extend(["-U", unquote(parsed.username)])
    dbname = parsed.path.lstrip("/") or "postgres"
    argv.append(dbname)
    return argv


def _psql_argv(db_url: str) -> list[str]:
    parsed = urlparse(db_url)
    argv = [
        "psql",
        "--no-psqlrc",
        "--single-transaction",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    if parsed.hostname:
        argv.extend(["-h", parsed.hostname])
    if parsed.port:
        argv.extend(["-p", str(parsed.port)])
    if parsed.username:
        argv.extend(["-U", unquote(parsed.username)])
    dbname = parsed.path.lstrip("/") or "postgres"
    argv.extend(["-d", dbname])
    return argv


def _pg_env(db_url: str, *, lock_timeout_ms: int | None = None) -> dict[str, str]:
    """Environment for pg_dump / psql: PATH plus libpq's own PG* variables.

    The server environment carries LLM keys, the API-key pepper and the session
    secret; the client tools need none of them, so nothing else is inherited.
    """
    parsed = urlparse(db_url)
    env = {key: value for key, value in os.environ.items() if key == "PATH" or key.startswith("PG")}
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    if lock_timeout_ms is not None:
        env["PGOPTIONS"] = f"-c lock_timeout={lock_timeout_ms}"
    return env


async def _run_pg_client(
    argv: list[str],
    env: dict[str, str],
    *,
    feed_stdin: Callable[[asyncio.StreamWriter], Coroutine[Any, Any, None]] | None = None,
    drain_stdout: Callable[[asyncio.StreamReader], Coroutine[Any, Any, None]] | None = None,
) -> tuple[int, bytes]:
    """Run a PostgreSQL client tool and return ``(returncode, stderr)``.

    stdin, stdout and stderr are serviced concurrently so a chatty stream can
    never fill its pipe and stall the others. The process is killed when the
    run exceeds ``PG_CLIENT_TIMEOUT_S`` (raising ``TimeoutError``) or when the
    caller is cancelled.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if feed_stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if drain_stdout else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stderr is not None
    try:
        async with asyncio.timeout(PG_CLIENT_TIMEOUT_S):
            async with asyncio.TaskGroup() as tg:
                stderr_task = tg.create_task(proc.stderr.read())
                if feed_stdin is not None:
                    assert proc.stdin is not None
                    tg.create_task(feed_stdin(proc.stdin))
                if drain_stdout is not None:
                    assert proc.stdout is not None
                    tg.create_task(drain_stdout(proc.stdout))
            returncode = await proc.wait()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return returncode, stderr_task.result()


class UnsafeDumpError(Exception):
    """The dump holds input psql could run as a client-side meta-command."""

    def __init__(self, line_number: int, reason: str) -> None:
        super().__init__(f"line {line_number}: {reason}")
        self.line_number = line_number
        self.reason = reason


class _LexState(Enum):
    CODE = auto()
    SINGLE_QUOTE = auto()
    DOUBLE_QUOTE = auto()
    BLOCK_COMMENT = auto()


_IDENT_START = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_" + bytes(range(0x80, 0x100))
)
_DIGITS = frozenset(b"0123456789")
_IDENT_CONT = _IDENT_START | _DIGITS | frozenset(b"$")
_NUMBER_CONT = _IDENT_START | _DIGITS | frozenset(b".")
_PSQL_VARIABLE_START = _IDENT_START | _DIGITS | frozenset(b"'\"{")
_WHITESPACE = frozenset(b" \t\n\r\f\v")

# pg_dump >= 17.6 / 16.10 frames plain dumps with these (CVE-2025-8714); they
# only narrow what psql accepts, so they are the one meta-command let through.
_RESTRICT_LINE_RE = re.compile(rb"\\(?:un)?restrict [A-Za-z0-9]+\r?\n?")
_COPY_FROM_STDIN_RE = re.compile(
    rb'COPY [A-Za-z0-9_."]+(?: \([A-Za-z0-9_", ]+\))? FROM stdin;\r?\n?'
)
_COPY_END_LINES = (b"\\.\n", b"\\.\r\n")


class _PsqlScriptScanner:
    """Conservative model of how psql splits a plain-format dump.

    Keep in step with the schema: the scanner accepts today's dumps only
    because no table DDL contains backslashes, ``$`` or ``BEGIN``. A migration
    that adds e.g. a regex CHECK constraint or a function makes every backup
    unrestorable (422 ``unsafe_pg_dump``) until the scanner models that syntax.

    psql treats a backslash outside quotes as the start of a meta-command
    anywhere on a line (``SELECT 1; \\! id`` runs a shell), while COPY data
    lines are passed through untouched and legitimately full of backslash
    escapes. The scanner therefore only needs to know, line by line, whether
    psql is reading COPY data. It follows psql's quoting, comment and
    parenthesis rules, and rejects the constructs whose statement boundaries it
    does not model — dollar quoting, psql variables, ``BEGIN ATOMIC`` bodies —
    so a mismatch can only refuse a restore, never let a command through. A
    COPY block is entered solely from a line psql executes as a complete
    ``COPY ... FROM stdin;``; if that COPY fails, ``ON_ERROR_STOP`` ends the
    run before psql reads the data lines.
    """

    def __init__(self) -> None:
        self._state = _LexState.CODE
        self._comment_depth = 0
        self._paren_depth = 0
        self._statement_open = False
        self._in_copy = False

    def feed(self, line: bytes, line_number: int) -> None:
        if self._in_copy:
            if line in _COPY_END_LINES:
                self._in_copy = False
            return
        if b"\x00" in line:
            raise UnsafeDumpError(line_number, "NUL byte in script text")
        at_statement_start = self._at_statement_start()
        if at_statement_start and _RESTRICT_LINE_RE.fullmatch(line):
            return
        if b"\\" in line:
            raise UnsafeDumpError(line_number, "backslash outside COPY data")
        self._lex(line, line_number)
        if (
            at_statement_start
            and _COPY_FROM_STDIN_RE.fullmatch(line)
            and self._at_statement_start()
        ):
            self._in_copy = True

    def _at_statement_start(self) -> bool:
        return self._state is _LexState.CODE and not self._statement_open and self._paren_depth == 0

    def _lex(self, line: bytes, line_number: int) -> None:
        i, n = 0, len(line)
        while i < n:
            if self._state is _LexState.SINGLE_QUOTE or self._state is _LexState.DOUBLE_QUOTE:
                quote = b"'" if self._state is _LexState.SINGLE_QUOTE else b'"'
                end = line.find(quote, i)
                if end < 0:
                    return
                self._state = _LexState.CODE
                i = end + 1
                continue
            if self._state is _LexState.BLOCK_COMMENT:
                if line.startswith(b"/*", i):
                    self._comment_depth += 1
                    i += 2
                elif line.startswith(b"*/", i):
                    self._comment_depth -= 1
                    if self._comment_depth == 0:
                        self._state = _LexState.CODE
                    i += 2
                else:
                    i += 1
                continue

            char = line[i]
            if char in _WHITESPACE:
                i += 1
                continue
            if line.startswith(b"--", i):
                return
            self._statement_open = True
            if line.startswith(b"/*", i):
                self._state = _LexState.BLOCK_COMMENT
                self._comment_depth = 1
                i += 2
            elif char == ord("'"):
                self._state = _LexState.SINGLE_QUOTE
                i += 1
            elif char == ord('"'):
                self._state = _LexState.DOUBLE_QUOTE
                i += 1
            elif char == ord("("):
                self._paren_depth += 1
                i += 1
            elif char == ord(")"):
                self._paren_depth = max(self._paren_depth - 1, 0)
                i += 1
            elif char == ord(";"):
                if self._paren_depth == 0:
                    self._statement_open = False
                i += 1
            elif char == ord(":"):
                following = line[i + 1] if i + 1 < n else None
                if following == ord(":"):
                    i += 2
                elif following is not None and following in _PSQL_VARIABLE_START:
                    raise UnsafeDumpError(line_number, "psql variable reference")
                else:
                    i += 1
            elif char == ord("$"):
                raise UnsafeDumpError(line_number, "dollar quote or parameter")
            elif char in _IDENT_START:
                end = _scan(line, i + 1, _IDENT_CONT)
                if line[i:end].lower() == b"begin":
                    raise UnsafeDumpError(line_number, "BEGIN block")
                i = end
            elif char in _DIGITS:
                i = _scan(line, i + 1, _NUMBER_CONT)
            else:
                i += 1


def _scan(line: bytes, start: int, allowed: frozenset[int]) -> int:
    end = start
    while end < len(line) and line[end] in allowed:
        end += 1
    return end


def _check_dump_safe(dump_path: Path) -> None:
    """Raise ``UnsafeDumpError`` unless psql would only run SQL from the dump."""
    scanner = _PsqlScriptScanner()
    with dump_path.open("rb") as fp:
        for line_number, line in enumerate(fp, start=1):
            scanner.feed(line, line_number)


class _ArchiveLinkError(Exception):
    """A restore archive carries a symlink or hard link member."""


def _restore_member_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
    # The data filter admits links that stay inside the extraction dir, but the
    # extracted trees are then moved one level up into fs_root, where a link
    # to the extraction root points outside fs_root. Backups never contain
    # links, so any link is tampering.
    if member.issym() or member.islnk():
        raise _ArchiveLinkError(member.name)
    return tarfile.data_filter(member, dest_path)


class _FsSwap:
    """Reversible replacement of fs_root entries.

    Each replaced entry is renamed into ``aside_dir`` (under fs_root, so the
    rename stays on one volume) instead of being deleted. ``rollback`` puts the
    parked entries back; ``commit`` deletes them.
    """

    def __init__(self, aside_dir: Path) -> None:
        self._aside_dir = aside_dir
        self._moves: list[tuple[Path, Path | None]] = []

    def replace(self, src: Path, target: Path) -> None:
        parked: Path | None = None
        if target.exists() or target.is_symlink():
            self._aside_dir.mkdir(parents=True, exist_ok=True)
            parked = self._aside_dir / str(len(self._moves))
            os.rename(target, parked)
        self._moves.append((target, parked))
        os.rename(src, target)

    def rollback(self) -> None:
        while self._moves:
            target, parked = self._moves[-1]
            _remove_entry(target)
            if parked is not None:
                os.rename(parked, target)
            self._moves.pop()
        shutil.rmtree(self._aside_dir, ignore_errors=True)

    def commit(self) -> None:
        self._moves.clear()
        shutil.rmtree(self._aside_dir, ignore_errors=True)


def _remove_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _swap_in_restored_trees(
    swap: _FsSwap, restored_root: Path, fs_root: Path, *, replace_trees: bool
) -> None:
    """Move the extracted fs trees into fs_root through ``swap``.

    With ``replace_trees`` (a forced restore over existing state) each tree
    present in the archive replaces the current tree wholesale; otherwise the
    archive's entries are merged in, replacing same-named entries — both trees
    hold plain files as well as directories (``blueprints/`` carries the
    image-sync manifest). A tree absent from the archive is left untouched.
    """
    for tree in FS_TREES:
        src = restored_root / tree
        if not src.is_dir():
            continue
        target = fs_root / tree
        if replace_trees:
            swap.replace(src, target)
            continue
        target.mkdir(parents=True, exist_ok=True)
        for item in sorted(src.iterdir()):
            swap.replace(item, target / item.name)


async def _rollback_fs_swap(swap: _FsSwap, aside_dir: Path) -> bool:
    """Undo ``swap``; return False (after logging) if the old trees could not be put back."""
    try:
        await asyncio.to_thread(swap.rollback)
    except Exception:
        log.exception("admin.restore.fs_rollback_failed", aside_dir=str(aside_dir))
        return False
    return True


def _wipe_statements() -> bytes:
    return "".join(f"DELETE FROM {table};\n" for table in PG_TABLES_FK_ORDER).encode()


async def _replay_dump(db_url: str, pg_dump_path: Path, *, wipe: bool) -> None:
    """Replay ``pg_dump_path`` through psql in a single transaction.

    With ``wipe`` the FK-ordered DELETEs run first inside that same
    transaction, so a failed replay leaves the existing rows in place.

    Raises:
        HTTPException: 500 ``psql_restore_failed`` on a non-zero exit or timeout.
    """
    prelude = _wipe_statements() if wipe else b""

    async def _feed(stdin: asyncio.StreamWriter) -> None:
        try:
            stdin.write(prelude)
            await stdin.drain()
            with pg_dump_path.open("rb") as dump_fp:
                while dump_chunk := dump_fp.read(UPLOAD_CHUNK_BYTES):
                    stdin.write(dump_chunk)
                    await stdin.drain()
        except BrokenPipeError, ConnectionResetError:
            # ON_ERROR_STOP makes psql exit mid-stream; its exit status and
            # stderr carry the actual failure.
            pass
        finally:
            stdin.close()

    try:
        returncode, psql_err = await _run_pg_client(
            _psql_argv(db_url),
            _pg_env(db_url, lock_timeout_ms=PSQL_LOCK_TIMEOUT_MS),
            feed_stdin=_feed,
        )
    except TimeoutError as exc:
        ADMIN_RESTORE_TOTAL.labels(outcome="psql_restore_failed").inc()
        log.error("admin.restore.psql_timeout", timeout_s=PG_CLIENT_TIMEOUT_S)
        raise HTTPException(
            500,
            {
                "error": "psql_restore_failed",
                "detail": f"psql did not finish within {PG_CLIENT_TIMEOUT_S}s",
            },
        ) from exc
    if returncode != 0:
        ADMIN_RESTORE_TOTAL.labels(outcome="psql_restore_failed").inc()
        stderr_text = psql_err.decode(errors="replace")
        log.error("admin.restore.psql_failed", stderr=stderr_text)
        raise HTTPException(
            500,
            {"error": "psql_restore_failed", "detail": stderr_text[:500]},
        )


async def _current_alembic_head(session: AsyncSession) -> str:
    row = await session.execute(text("SELECT version_num FROM alembic_version"))
    value = row.scalar_one_or_none()
    return value or "unknown"


async def _run_pg_dump(db_url: str, out_path: Path) -> None:
    """Dump the KeenySpace tables to ``out_path``.

    Raises:
        HTTPException: 500 ``pg_dump_failed`` on a non-zero exit or timeout.
    """

    # Stream pg_dump stdout to disk in chunks instead of buffering the whole
    # dump; a multi-GB database would otherwise pin O(dump_size) RSS.
    async def _drain(stdout: asyncio.StreamReader) -> None:
        with out_path.open("wb") as pg_fp:
            while pg_chunk := await stdout.read(UPLOAD_CHUNK_BYTES):
                await asyncio.to_thread(pg_fp.write, pg_chunk)

    try:
        returncode, pg_err = await _run_pg_client(
            _pg_dump_argv(db_url), _pg_env(db_url), drain_stdout=_drain
        )
    except TimeoutError as exc:
        log.error("admin.backup.pg_dump_timeout", timeout_s=PG_CLIENT_TIMEOUT_S)
        raise HTTPException(500, {"error": "pg_dump_failed"}) from exc
    if returncode != 0:
        log.error(
            "admin.backup.pg_dump_failed",
            stderr=pg_err.decode(errors="replace"),
        )
        raise HTTPException(500, {"error": "pg_dump_failed"})


def _backup_member_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if ".obsidian" in info.name.split("/"):
        return None
    if info.issym() or info.islnk():
        return None
    return info


def _sorted_dir_names(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(d.name for d in directory.iterdir() if d.is_dir())


def _write_backup_archive(
    archive_path: Path,
    pg_dump_path: Path,
    fs_root: Path,
    *,
    alembic_head: str,
    created_by: str,
) -> int:
    """Write the backup tarball to ``archive_path``; return the workspace count.

    Blocking (tree walk, file reads, gzip) — run it off the event loop.
    """
    workspaces_dir = fs_root / "workspaces"
    blueprints_dir = fs_root / "blueprints"
    ws_uuids = _sorted_dir_names(workspaces_dir)
    bp_names = _sorted_dir_names(blueprints_dir)
    fs_root_size = (
        sum(p.stat().st_size for p in workspaces_dir.rglob("*") if p.is_file())
        if workspaces_dir.exists()
        else 0
    )
    manifest = BackupManifest(
        version=1,
        keenyspace_version=KS_VERSION,
        schema_version=1,
        alembic_head=alembic_head,
        created_at=datetime.now(UTC),
        created_by=created_by,
        fs_root_size_bytes=fs_root_size,
        workspaces={"count": len(ws_uuids), "uuids": ws_uuids},
        blueprints={"count": len(bp_names), "names": bp_names},
        pg_tables_dumped=list(PG_TABLES_DUMPED),
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode()
    now_ts = int(datetime.now(UTC).timestamp())

    with tarfile.open(archive_path, mode="w:gz") as tar:
        manifest_info = tarfile.TarInfo(name="manifest.json")
        manifest_info.size = len(manifest_bytes)
        manifest_info.mtime = now_ts
        tar.addfile(manifest_info, io.BytesIO(manifest_bytes))

        pg_info = tarfile.TarInfo(name="pg_dump.sql")
        pg_info.size = pg_dump_path.stat().st_size
        pg_info.mtime = now_ts
        with pg_dump_path.open("rb") as fp:
            tar.addfile(pg_info, fp)

        for tree_dir in (workspaces_dir, blueprints_dir):
            if tree_dir.exists():
                tar.add(
                    str(tree_dir),
                    arcname=f"fs_root/{tree_dir.name}",
                    filter=_backup_member_filter,
                )
    return len(ws_uuids)


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
    alembic_head = await _current_alembic_head(session)
    await write_audit(
        session,
        actor_sub=user.sub,
        action="admin.backup.requested",
        payload={"workspace_count": int(ws_count)},
    )
    # Ends the request's only transaction: nothing may keep a pooled connection
    # (and its ACCESS SHARE locks) idle-in-transaction through pg_dump.
    await session.commit()
    db_url = settings.db.url

    tmp_dir = fs_root / "tmp" / f"backup-{secrets.token_hex(8)}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pg_dump_path = tmp_dir / "pg_dump.sql"
    archive_path = tmp_dir / "backup.tar.gz"
    # The archive is complete on disk BEFORE the StreamingResponse is returned:
    # a failure raised inside the streaming generator cannot un-send the 200 +
    # headers already on the wire, so the client would receive a truncated
    # "successful" backup (e.g. a pg_dump client older than the server).
    try:
        await _run_pg_dump(db_url, pg_dump_path)
        workspace_count = await asyncio.to_thread(
            _write_backup_archive,
            archive_path,
            pg_dump_path,
            fs_root,
            alembic_head=alembic_head,
            created_by=user.sub,
        )
        archive_fp = archive_path.open("rb")
    finally:
        # Starlette never closes a body iterator it has not started, so cleanup
        # cannot live in the generator: a client gone before the first chunk
        # would leave a full-size archive on the vault volume. The open handle
        # keeps the unlinked archive readable until the stream finishes or the
        # handle is garbage-collected.
        await asyncio.to_thread(shutil.rmtree, tmp_dir, True)
    archive_size = os.fstat(archive_fp.fileno()).st_size

    async def _stream() -> AsyncIterator[bytes]:
        total_bytes = 0
        try:
            while chunk := await asyncio.to_thread(archive_fp.read, UPLOAD_CHUNK_BYTES):
                total_bytes += len(chunk)
                yield chunk
            ADMIN_BACKUP_BYTES.inc(total_bytes)
            ADMIN_BACKUP_TOTAL.inc()
            log.info(
                "admin.backup.completed",
                user_sub=user.sub,
                total_bytes=total_bytes,
                workspace_count=workspace_count,
            )
        finally:
            archive_fp.close()

    iso = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": (f'attachment; filename="keenyspace-backup-{iso}.tar.gz"'),
            "Content-Length": str(archive_size),
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
    aside_dir = tmp_parent / f"{tmp_dir.name}.aside"
    try:
        with archive_path.open("wb") as fp:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                await asyncio.to_thread(fp.write, chunk)

        # tar.extractall walks the entire archive synchronously (file IO +
        # writes). Offload to a worker thread so the event loop can continue
        # serving health probes and concurrent requests during a large restore.
        def _extract_tar() -> None:
            with tarfile.open(archive_path, "r:gz") as tar:
                # The data filter is applied explicitly (inside
                # _restore_member_filter) so a future stdlib default change
                # cannot silently widen the attack surface.
                tar.extractall(path=tmp_dir, filter=_restore_member_filter)

        try:
            await asyncio.to_thread(_extract_tar)
        except _ArchiveLinkError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="symlink").inc()
            raise HTTPException(422, {"error": "symlink", "detail": f"link member {exc}"}) from exc
        except tarfile.OutsideDestinationError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="path_traversal").inc()
            raise HTTPException(422, {"error": "path_traversal", "detail": str(exc)}) from exc
        except tarfile.AbsolutePathError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="absolute_path").inc()
            raise HTTPException(422, {"error": "absolute_path", "detail": str(exc)}) from exc
        except tarfile.LinkOutsideDestinationError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="symlink").inc()
            raise HTTPException(422, {"error": "symlink", "detail": str(exc)}) from exc
        except tarfile.TarError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="malformed").inc()
            raise HTTPException(422, {"error": "malformed_tar", "detail": str(exc)}) from exc

        manifest_path = tmp_dir / "manifest.json"
        if not manifest_path.exists():
            ADMIN_RESTORE_TOTAL.labels(outcome="missing_manifest").inc()
            raise HTTPException(422, {"error": "missing_manifest"})
        manifest = BackupManifest.model_validate_json(manifest_path.read_text())

        try:
            source_version = _semver.VersionInfo.parse(manifest.keenyspace_version)
            target_version = _semver.VersionInfo.parse(KS_VERSION)
        except ValueError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="bad_version").inc()
            raise HTTPException(422, {"error": "bad_version", "detail": str(exc)}) from exc
        if (source_version.major, source_version.minor) != (
            target_version.major,
            target_version.minor,
        ) and not force:
            ADMIN_RESTORE_TOTAL.labels(outcome="version_mismatch").inc()
            raise HTTPException(
                422,
                {
                    "error": "version_mismatch",
                    "source": str(source_version),
                    "target": str(target_version),
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

        existing = (await session.execute(text("SELECT count(*) FROM workspaces"))).scalar_one()
        existing = int(existing)
        existing_dirs = _sorted_dir_names(fs_root / "workspaces")
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

        pg_dump_path = tmp_dir / "pg_dump.sql"
        if not pg_dump_path.is_file():
            ADMIN_RESTORE_TOTAL.labels(outcome="missing_pg_dump").inc()
            raise HTTPException(422, {"error": "missing_pg_dump"})
        restored_root = tmp_dir / "fs_root"
        if not (restored_root / "workspaces").is_dir():
            ADMIN_RESTORE_TOTAL.labels(outcome="missing_fs_tree").inc()
            raise HTTPException(422, {"error": "missing_fs_tree", "detail": "fs_root/workspaces"})
        try:
            await asyncio.to_thread(_check_dump_safe, pg_dump_path)
        except UnsafeDumpError as exc:
            ADMIN_RESTORE_TOTAL.labels(outcome="unsafe_pg_dump").inc()
            log.warning(
                "admin.restore.unsafe_pg_dump",
                user_sub=user.sub,
                line_number=exc.line_number,
                reason=exc.reason,
            )
            raise HTTPException(422, {"error": "unsafe_pg_dump", "detail": str(exc)}) from exc

        wipe = force and (existing > 0 or bool(existing_dirs))

        # This request's own session still holds the ACCESS SHARE locks taken by
        # the reads above (alembic_version, workspaces). The dump replays with
        # --clean, whose DROP TABLE needs ACCESS EXCLUSIVE, so psql would wait
        # on our transaction forever — silently, with the connection healthy.
        await session.commit()

        swap = _FsSwap(aside_dir)
        try:
            await asyncio.to_thread(
                _swap_in_restored_trees,
                swap,
                restored_root,
                fs_root,
                replace_trees=wipe,
            )
            await _replay_dump(db_url, pg_dump_path, wipe=wipe)
        except BaseException as exc:
            rolled_back = await _rollback_fs_swap(swap, aside_dir)
            if not rolled_back and isinstance(exc, Exception):
                ADMIN_RESTORE_TOTAL.labels(outcome="rollback_failed").inc()
                raise HTTPException(
                    500,
                    {
                        "error": "restore_rollback_failed",
                        "detail": (
                            "restore failed and the previous fs trees could not "
                            f"be moved back; they are preserved in {aside_dir}"
                        ),
                    },
                ) from exc
            raise

        # psql replayed a --clean dump: every table the pool's connections have
        # touched was dropped and recreated underneath them, invalidating the
        # cached statements asyncpg holds per connection. Drop the pool so the
        # writes below run on connections that have seen the restored schema.
        engine = get_engine()
        if engine is not None:
            await engine.dispose()
        # api_keys and users now hold the backup's rows; cached key
        # verifications and snapshot debounce state describe the old ones.
        api_key_service: ApiKeyService = request.app.state.api_key_service
        api_key_service.forget_all()
        group_snapshots: GroupSnapshotStore = request.app.state.group_snapshots
        group_snapshots.forget_all()

        await asyncio.to_thread(swap.commit)

        if wipe:
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
    except HTTPException:
        raise
    except Exception as exc:
        # Without this handler an unexpected failure surfaces as Starlette's
        # bare "Internal Server Error" with nothing in the logs, which makes a
        # failed restore undiagnosable.
        ADMIN_RESTORE_TOTAL.labels(outcome="unexpected_error").inc()
        log.exception("admin.restore.unexpected_error", error=str(exc))
        raise HTTPException(500, {"error": "restore_failed", "detail": str(exc)[:500]}) from exc
    finally:
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/api-keys/revoke-all", response_model=ApiKeyRevokeAllResponse)
async def admin_revoke_user_api_keys(
    body: ApiKeyRevokeAllRequest, request: Request
) -> ApiKeyRevokeAllResponse:
    service: ApiKeyService = request.app.state.api_key_service
    revoked = await service.revoke_all_for_user(body.sub, actor_sub=request.user.sub)
    group_snapshots: GroupSnapshotStore = request.app.state.group_snapshots
    group_snapshots.forget(body.sub)
    return ApiKeyRevokeAllResponse(sub=body.sub, revoked=revoked)
