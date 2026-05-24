"""XDG-respecting filesystem layout for the keenyspace CLI.

Per 05-RESEARCH §8 Option B: ignore `platformdirs` on macOS to keep the
Linux-style `~/.config/keenyspace` / `~/.local/state/keenyspace` layout
locked by PROJECT.md. The XDG env-var helpers exist so the test fixture
`temp_config_dir` (Plan 01) can isolate the layout without monkeypatching
`Path.home`.
"""

from __future__ import annotations

import os
from pathlib import Path


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".config"


def _xdg_state_home() -> Path:
    raw = os.environ.get("XDG_STATE_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "state"


CONFIG_DIR = _xdg_config_home() / "keenyspace"
STATE_DIR = _xdg_state_home() / "keenyspace"

CONFIG_YAML = CONFIG_DIR / "config.yaml"
AUTH_JSON = CONFIG_DIR / "auth.json"
WORKSPACE_MAP_YAML = CONFIG_DIR / "workspace-map.yaml"
KILL_SWITCH = CONFIG_DIR / "disabled"

DAEMON_SOCK = STATE_DIR / "daemon.sock"
DAEMON_LOG = STATE_DIR / "daemon.log"
DAEMON_PID = STATE_DIR / "daemon.pid"
DROPPED_JSON = STATE_DIR / "dropped.json"

DEFAULT_PULL_ROOT = Path.home() / "keenyspace"
