"""asyncio UDS daemon — JSONL dispatch with mode-0600 socket + kill switch.

Per Phase 5 D-06/D-07/D-09 + 05-RESEARCH §9. The umask manipulation + chmod
combo guarantees socket file mode 0o600 even under user umasks that would
otherwise leave it world-readable (T-05.05-02 mitigation).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal

import structlog

from keenyspace.paths import (
    DAEMON_LOG,
    DAEMON_PID,
    DAEMON_SOCK,
    KILL_SWITCH,
    STATE_DIR,
)

log = structlog.get_logger(__name__)


def _configure_file_logging() -> None:
    """Send structlog output to ~/.local/state/keenyspace/daemon.log.

    Re-entrant safe: only attaches the handler once per process. Stays
    optional — if directory creation fails, fall back to stderr.
    """
    root = logging.getLogger()
    handler_path = str(DAEMON_LOG.resolve()) if DAEMON_LOG.exists() else str(DAEMON_LOG)
    for h in root.handlers:
        if getattr(h, "_keenyspace_daemon_log", False):
            return
    try:
        DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(handler_path, encoding="utf-8")
        handler._keenyspace_daemon_log = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except OSError:
        pass
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


async def serve() -> None:
    _configure_file_logging()
    if KILL_SWITCH.exists():
        log.info("daemon.killswitch_active", path=str(KILL_SWITCH))
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    if DAEMON_SOCK.exists():
        DAEMON_SOCK.unlink()
    old_umask = os.umask(0o077)
    try:
        server = await asyncio.start_unix_server(_handle, path=str(DAEMON_SOCK))
    finally:
        os.umask(old_umask)
    DAEMON_SOCK.chmod(0o600)
    DAEMON_PID.write_text(str(os.getpid()))
    DAEMON_PID.chmod(0o600)
    log.info("daemon.started", sock=str(DAEMON_SOCK), pid=os.getpid())

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows / restricted runtime: signal handlers may be unavailable;
        # the daemon is out of scope on those platforms per CONTEXT.md.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    try:
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            await stop_event.wait()
            serve_task.cancel()
            try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
                await serve_task
            except asyncio.CancelledError:
                pass
    finally:
        DAEMON_SOCK.unlink(missing_ok=True)
        DAEMON_PID.unlink(missing_ok=True)
        log.info("daemon.stopped")


async def _handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    from keenyspace.daemon.handlers import dispatch

    try:
        line = await reader.readline()
        if not line:
            return
        envelope = json.loads(line.decode())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("daemon.envelope_parse_failed", err=str(exc))
        _close(writer)
        return
    try:
        await dispatch(envelope, writer)
    except Exception as exc:  # broad: daemon must survive any handler error
        log.warning("daemon.handler_failed", err=str(exc))
    _close(writer)


def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
