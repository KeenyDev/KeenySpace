from __future__ import annotations

import os
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import yaml

from .atomic import write_atomic


def clone_default_blueprint(
    fs_root: Path,
    blueprint_name: str,
    ws_uuid: UUID,
    slug: str = "",
    display_name: str = "",
) -> Path:
    src = fs_root / "blueprints" / blueprint_name
    final = fs_root / "workspaces" / str(ws_uuid)
    tmp = final.parent / f"{ws_uuid}.tmp.{secrets.token_hex(8)}"

    shutil.copytree(
        src,
        tmp,
        symlinks=False,
        dirs_exist_ok=False,
        ignore_dangling_symlinks=True,
    )
    os.replace(tmp, final)

    _write_workspace_config(
        final,
        ws_uuid,
        slug or str(ws_uuid),
        display_name or str(ws_uuid),
        f"{blueprint_name}@v0.1",
    )
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
