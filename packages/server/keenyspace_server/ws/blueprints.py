from __future__ import annotations

import asyncio
import os
from pathlib import Path

import structlog
import yaml
from keenyspace_shared.mcp_contracts import BlueprintInfo

log = structlog.get_logger(__name__)


async def list_blueprints_from_fs(fs_root: Path) -> list[BlueprintInfo]:
    bp_dir = fs_root / "blueprints"
    if not bp_dir.is_dir():
        return []

    def _scan() -> list[BlueprintInfo]:
        results: list[BlueprintInfo] = []
        with os.scandir(bp_dir) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                yaml_path = Path(entry.path) / ".keenyspace" / "blueprint.yaml"
                if not yaml_path.exists():
                    continue
                try:
                    data = yaml.safe_load(yaml_path.read_text())
                except Exception as exc:
                    log.warning(
                        "blueprint.yaml_parse_failed",
                        path=str(yaml_path),
                        error=str(exc),
                    )
                    continue
                if not isinstance(data, dict):
                    log.warning("blueprint.yaml_invalid_shape", path=str(yaml_path))
                    continue
                results.append(
                    BlueprintInfo(
                        name=entry.name,
                        version=str(data.get("version", "unknown")),
                        description=str(data.get("description", "")),
                    )
                )
        results.sort(key=lambda b: b.name)
        return results

    return await asyncio.to_thread(_scan)
