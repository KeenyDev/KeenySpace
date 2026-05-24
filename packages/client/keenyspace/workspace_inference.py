"""HK-11 + D-13: resolve workspace slug from cwd.

Precedence (highest -> lowest):
  1. explicit arg
  2. env var KEENYSPACE_WORKSPACE
  3. walk-up search for .keenyspace/slug-marker.json (D-13 option b)
  4. workspace-map.yaml longest-prefix match
  5. default_workspace from config.yaml

When nothing matches, returns None and the caller decides whether to error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from keenyspace.paths import WORKSPACE_MAP_YAML


def resolve_workspace_slug(
    cwd: str | Path | None = None,
    *,
    explicit: str | None = None,
) -> tuple[str | None, str]:
    """Return (slug, source) where source is one of: explicit | env | slug-marker
    | workspace-map | default | unresolved.
    """
    if explicit:
        return explicit, "explicit"
    env_slug = os.environ.get("KEENYSPACE_WORKSPACE")
    if env_slug:
        return env_slug, "env"
    cwd_path = Path(cwd or os.getcwd()).resolve()
    marker_slug = _walk_up_slug_marker(cwd_path)
    if marker_slug is not None:
        return marker_slug, "slug-marker"
    mapped = _lookup_workspace_map(cwd_path)
    if mapped is not None:
        return mapped, "workspace-map"
    from keenyspace.config import get_client_settings

    default = get_client_settings().default_workspace
    if default:
        return default, "default"
    return None, "unresolved"


def _walk_up_slug_marker(cwd: Path) -> str | None:
    for parent in (cwd, *cwd.parents):
        marker = parent / ".keenyspace" / "slug-marker.json"
        if not marker.is_file():
            continue
        try:
            data: Any = json.loads(marker.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            slug = data.get("slug")
            if isinstance(slug, str) and slug:
                return slug
    return None


def _lookup_workspace_map(cwd: Path) -> str | None:
    if not WORKSPACE_MAP_YAML.is_file():
        return None
    try:
        raw = yaml.safe_load(WORKSPACE_MAP_YAML.read_text()) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    paths_map_raw = raw.get("paths", {}) or {}
    if not isinstance(paths_map_raw, dict):
        return None
    best_prefix_len = -1
    best_slug: str | None = None
    for prefix_str, slug_val in paths_map_raw.items():
        if not isinstance(prefix_str, str) or not isinstance(slug_val, str):
            continue
        try:
            expanded = Path(os.path.expanduser(prefix_str)).resolve()
        except (OSError, RuntimeError):
            continue
        try:
            cwd.relative_to(expanded)
        except ValueError:
            continue
        if len(str(expanded)) > best_prefix_len:
            best_prefix_len = len(str(expanded))
            best_slug = slug_val
    return best_slug
