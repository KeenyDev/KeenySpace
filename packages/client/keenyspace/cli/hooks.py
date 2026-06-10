from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import typer

hooks_app = typer.Typer(name="hooks", help="Manage KeenySpace hooks in Claude Code settings.json")

# WHY trailing space: prevents a future "keenyspace hooks ..." command from being
# mis-detected as a hook entry when checking command.startswith(OURS_PREFIX).
OURS_PREFIX = "keenyspace hook "

KEENYSPACE_HOOKS: dict[str, list[dict[str, Any]]] = {
    "SessionStart": [
        {
            "matcher": "compact",
            "hooks": [
                {
                    "type": "command",
                    "command": "keenyspace hook session-start",
                    "timeout": 30,
                    "statusMessage": "Re-injecting KeenySpace workspace context after compaction",
                }
            ],
        },
        {
            "matcher": "startup|resume|clear",
            "hooks": [
                {
                    "type": "command",
                    "command": "keenyspace hook session-start",
                    "async": True,
                }
            ],
        },
    ],
    "SessionEnd": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "keenyspace hook session-end",
                    "async": True,
                }
            ]
        }
    ],
    "PreCompact": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "keenyspace hook pre-compact",
                    "async": True,
                }
            ]
        }
    ],
    "PostCompact": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "keenyspace hook post-compact",
                    "async": True,
                }
            ]
        }
    ],
    "PostToolUse": [
        {
            "matcher": "Edit|Write|mcp__keenyspace__.*",
            "hooks": [
                {
                    "type": "command",
                    "command": "keenyspace hook post-tool",
                    "async": True,
                }
            ],
        }
    ],
}


def _is_ours(group: dict[str, Any]) -> bool:
    return any(
        str(obj.get("command", "")).startswith(OURS_PREFIX)
        for obj in group.get("hooks", [])
    )


def _merge(settings: dict[str, Any]) -> dict[str, Any]:
    settings = copy.deepcopy(settings)
    if "hooks" not in settings:
        settings["hooks"] = {}
    for event, canonical_groups in KEENYSPACE_HOOKS.items():
        existing = settings["hooks"].get(event, [])
        foreign = [g for g in existing if not _is_ours(g)]
        settings["hooks"][event] = foreign + copy.deepcopy(canonical_groups)
    return settings


def _prune(settings: dict[str, Any]) -> dict[str, Any]:
    settings = copy.deepcopy(settings)
    if "hooks" not in settings:
        return settings
    for event in list(settings["hooks"].keys()):
        settings["hooks"][event] = [
            g for g in settings["hooks"][event] if not _is_ours(g)
        ]
        if not settings["hooks"][event]:
            del settings["hooks"][event]
    if not settings["hooks"]:
        del settings["hooks"]
    return settings


def _status(settings: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    hooks_section = settings.get("hooks", {})
    for event, canonical_groups in KEENYSPACE_HOOKS.items():
        present = [g for g in hooks_section.get(event, []) if _is_ours(g)]
        if not present:
            result[event] = "not installed"
        elif present == canonical_groups:
            result[event] = "installed"
        else:
            result[event] = "partial"
    return result


def _target_path(project: str | None) -> Path:
    # WHY Path.home(): resilient against unset HOME (chroot, restricted shells,
    # custom PAM) — os.environ["HOME"] would raise KeyError.
    if project is None:
        return Path.home() / ".claude" / "settings.json"
    return Path(project).resolve() / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{path} is not valid JSON; refusing to overwrite") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{path} is not valid JSON; refusing to overwrite")
    return parsed


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    from keenyspace.fs.atomic import write_atomic

    write_atomic(path, (json.dumps(settings, indent=2) + "\n").encode())


@hooks_app.command("install")
def install(
    project: str | None = typer.Option(None, "--project"),
) -> None:
    """Install KeenySpace hooks into Claude Code settings.json."""
    path = _target_path(project)
    settings = _load_settings(path)
    merged = _merge(settings)
    _write_settings(path, merged)
    typer.echo(f"installed keenyspace hooks -> {path}")


@hooks_app.command("uninstall")
def uninstall(
    project: str | None = typer.Option(None, "--project"),
) -> None:
    """Remove KeenySpace hooks from Claude Code settings.json."""
    path = _target_path(project)
    settings = _load_settings(path)
    if "hooks" not in settings:
        typer.echo("nothing to uninstall")
        return
    pruned = _prune(settings)
    _write_settings(path, pruned)
    typer.echo(f"removed keenyspace hooks <- {path}")


@hooks_app.command("status")
def status(
    project: str | None = typer.Option(None, "--project"),
) -> None:
    """Show installation status of KeenySpace hooks."""
    path = _target_path(project)
    settings = _load_settings(path)
    for event, state in _status(settings).items():
        typer.echo(f"{event}: {state}")
