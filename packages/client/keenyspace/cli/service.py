"""`keenyspace service install/uninstall` — register the daemon with launchd or systemd-user.

`shutil.which("keenyspace")` resolves to an absolute path, which the template
substitutes for `__KEENYSPACE_BIN__`. Re-run `service install` after
`uv tool upgrade keenyspace` so the launchd / systemd unit picks up the new
path.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import typer

from keenyspace.fs.atomic import write_atomic_secret

service_app = typer.Typer(name="service", help="OS service registration")

LAUNCHD_LABEL = "com.keenyspace.daemon"
SYSTEMD_UNIT = "keenyspace.service"


def _resolve_binary() -> Path:
    bin_path = shutil.which("keenyspace")
    if not bin_path:
        raise typer.BadParameter(
            "keenyspace binary not found on PATH; install via `uv tool install keenyspace` first"
        )
    return Path(bin_path).resolve()


def _read_template(name: str) -> str:
    return files("keenyspace.templates").joinpath(name).read_text(encoding="utf-8")


def _install_macos() -> None:
    # Path.home() is resilient against unset HOME (chroot, restricted shells,
    # custom PAM) — os.environ["HOME"] would raise KeyError. The rest of this
    # codebase already uses Path.home() in paths.py.
    plist = (
        _read_template("launchd_com.keenyspace.daemon.plist")
        .replace("__KEENYSPACE_BIN__", str(_resolve_binary()))
        .replace("__HOME__", str(Path.home()))
    )
    dest = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    # 0600: documented setup puts the LLM API key in EnvironmentVariables.
    write_atomic_secret(dest, plist.encode("utf-8"))
    uid = os.getuid()
    subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(dest)], check=True)
    subprocess.run(["/bin/launchctl", "enable", f"gui/{uid}/{LAUNCHD_LABEL}"], check=True)
    typer.echo(f"installed {dest}")


def _install_linux() -> None:
    unit = _read_template("systemd_keenyspace.service").replace(
        "__KEENYSPACE_BIN__", str(_resolve_binary())
    )
    dest = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], check=True)
    typer.echo(f"installed {dest}")


def _uninstall_macos() -> None:
    uid = os.getuid()
    subprocess.run(["/bin/launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], check=False)
    dest = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    dest.unlink(missing_ok=True)
    typer.echo("uninstalled launchd agent")


def _uninstall_linux() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], check=False)
    dest = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    dest.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    typer.echo("uninstalled systemd-user unit")


@service_app.command("install")
def service_install() -> None:
    """Register the keenyspace daemon with launchd (macOS) or systemd-user (Linux)."""
    sysname = platform.system()
    if sysname == "Darwin":
        _install_macos()
    elif sysname == "Linux":
        _install_linux()
    else:
        raise typer.BadParameter(f"unsupported OS: {sysname} (Windows daemon deferred to v1.5+)")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Unregister the keenyspace daemon from the OS service manager."""
    sysname = platform.system()
    if sysname == "Darwin":
        _uninstall_macos()
    elif sysname == "Linux":
        _uninstall_linux()
    else:
        raise typer.BadParameter(f"unsupported OS: {sysname}")
