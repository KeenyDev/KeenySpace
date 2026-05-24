"""Typer commands for the daemon lifecycle.

Top-level imports are deliberately limited to the stdlib + typer so that
`keenyspace --help` cold-boot stays under 600ms (Pitfall #1). asyncio is
required for asyncio.run inside `daemon start`; importing it at module
top is acceptable — it's stdlib, fast.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

import typer

from keenyspace.paths import DAEMON_PID, DAEMON_SOCK

daemon_app = typer.Typer(name="daemon", help="Background daemon controls")


@daemon_app.command("start")
def daemon_start(
    foreground: bool = typer.Option(
        True,
        "--foreground/--background",
        help="Run in foreground (default) or daemonise via double-fork.",
    ),
) -> None:
    """Bind the UDS daemon socket and run until SIGTERM."""
    from keenyspace.daemon.server import serve

    if not foreground:
        _double_fork()
    asyncio.run(serve())


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Send SIGTERM to the running daemon (pidfile)."""
    if not DAEMON_PID.exists():
        typer.echo("daemon not running (no pidfile)", err=True)
        raise typer.Exit(1)
    try:
        pid = int(DAEMON_PID.read_text().strip())
    except (OSError, ValueError) as exc:
        typer.echo(f"unreadable pidfile: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        DAEMON_PID.unlink(missing_ok=True)
        DAEMON_SOCK.unlink(missing_ok=True)
        typer.echo("daemon already exited; cleaned stale state", err=True)
        raise typer.Exit(0) from None
    # Wait up to 5s for socket to disappear
    for _ in range(50):
        time.sleep(0.1)
        if not DAEMON_SOCK.exists():
            return
    typer.echo("daemon did not exit within 5s; pid still alive", err=True)
    raise typer.Exit(1)


@daemon_app.command("status")
def daemon_status() -> None:
    """Exit 0 if daemon is reachable (pidfile + socket + live PID)."""
    if not DAEMON_PID.exists():
        typer.echo("not running", err=True)
        raise typer.Exit(1)
    if not DAEMON_SOCK.exists():
        typer.echo("pidfile present but socket missing", err=True)
        raise typer.Exit(1)
    try:
        pid = int(DAEMON_PID.read_text().strip())
    except (OSError, ValueError) as exc:
        typer.echo(f"unreadable pidfile: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        os.kill(pid, 0)
    except ProcessLookupError as exc:
        typer.echo("pidfile stale (process not running)", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"running pid={pid} sock={DAEMON_SOCK}")


def _double_fork() -> None:
    # POSIX double-fork: detach from controlling terminal so the daemon
    # survives the launching shell. Acceptable as a v1 dev convenience —
    # production deploys use launchd / systemd-user per D-07.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "rb") as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
