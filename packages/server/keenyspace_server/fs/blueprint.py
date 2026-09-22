from __future__ import annotations

import os
import re
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import structlog
import yaml

from .atomic import write_atomic

log = structlog.get_logger(__name__)

BLUEPRINT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_BLUEPRINT_NAME_RE = re.compile(BLUEPRINT_NAME_PATTERN)


class InvalidBlueprintNameError(ValueError):
    """The blueprint name is not a single safe path segment."""


class UnknownBlueprintError(LookupError):
    """No blueprint directory with the requested name exists under fs_root."""


def _resolve_blueprint_dir(fs_root: Path, blueprint_name: str) -> Path:
    if not _BLUEPRINT_NAME_RE.fullmatch(blueprint_name):
        raise InvalidBlueprintNameError(blueprint_name)
    blueprints_root = (fs_root / "blueprints").resolve()
    src = (blueprints_root / blueprint_name).resolve()
    if not src.is_relative_to(blueprints_root) or not src.is_dir():
        raise UnknownBlueprintError(blueprint_name)
    return src


def _ignore_symlinks(directory: str, names: list[str]) -> set[str]:
    # copytree(symlinks=False) would copy a link's target, letting a link planted
    # in a blueprint (e.g. via restore) pull files from outside it into a vault.
    return {name for name in names if os.path.islink(os.path.join(directory, name))}


def _move_instructions_to_keenyspace(ws_dir: Path) -> None:
    instructions_src = ws_dir / "_instructions"
    if not instructions_src.is_dir():
        return
    keenyspace_dir = ws_dir / ".keenyspace"
    keenyspace_dir.mkdir(parents=True, exist_ok=True)
    instructions_dst = keenyspace_dir / "instructions"
    if instructions_dst.exists():
        log.warning(
            "blueprint.instructions_dst_already_exists",
            ws_dir=str(ws_dir),
            instructions_src=str(instructions_src),
            instructions_dst=str(instructions_dst),
        )
        return
    # os.replace is idempotent and avoids the TOCTOU race between the exists()
    # check above and the move below; on POSIX it atomically overwrites a
    # destination of the same type.
    os.replace(instructions_src, instructions_dst)


def clone_default_blueprint(
    fs_root: Path,
    blueprint_name: str,
    ws_uuid: UUID,
    slug: str = "",
    display_name: str = "",
) -> Path:
    """Copy blueprint ``blueprint_name`` into a new workspace directory.

    Raises:
        InvalidBlueprintNameError: the name is not a single ``[a-z0-9_-]`` segment.
        UnknownBlueprintError: no such blueprint directory exists.
    """
    src = _resolve_blueprint_dir(fs_root, blueprint_name)
    final = fs_root / "workspaces" / str(ws_uuid)
    tmp = final.parent / f"{ws_uuid}.tmp.{secrets.token_hex(8)}"

    shutil.copytree(
        src,
        tmp,
        symlinks=False,
        dirs_exist_ok=False,
        ignore=_ignore_symlinks,
    )
    os.replace(tmp, final)
    # Write workspace config BEFORE moving _instructions/ so that a failure
    # mid-move (or an early-return on already-exists) doesn't leave a workspace
    # with no .keenyspace/config.yaml. Config mkdirs .keenyspace/ first.
    _write_workspace_config(
        final,
        ws_uuid,
        slug or str(ws_uuid),
        display_name or str(ws_uuid),
        f"{blueprint_name}@v0.1",
    )
    _move_instructions_to_keenyspace(final)
    return final


def _write_workspace_config(
    ws_dir: Path,
    ws_uuid: UUID,
    slug: str,
    display_name: str,
    blueprint_ref: str,
) -> None:
    config_dir = ws_dir / ".keenyspace"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "uuid": str(ws_uuid),
        "slug": slug,
        "display_name": display_name,
        "blueprint": blueprint_ref,
        "created_at": datetime.now(UTC).isoformat(),
        "schema_version": 1,
    }
    config_path = config_dir / "config.yaml"
    write_atomic(config_path, yaml.dump(config, allow_unicode=True).encode())
