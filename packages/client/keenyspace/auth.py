"""Token persistence + auth.json mode-0600 invariant.

Per 05-RESEARCH §13 / D-01: refuse to start the CLI when auth.json has
any group/world bit set on Unix. Mirrors server `auth/api_keys.py`
KEY_PREFIX so the client can detect `ks_live_*` API keys versus OIDC
bearers without a server round-trip.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

from keenyspace.fs.atomic import write_atomic_secret
from keenyspace.paths import AUTH_JSON

KEY_PREFIX = "ks_live_"


class AuthFileModeError(SystemExit):
    pass


def is_api_key(token: str) -> bool:
    return token.startswith(KEY_PREFIX)


def _validate_auth_file_mode(path: Path = AUTH_JSON) -> None:
    if sys.platform == "win32":
        return
    if not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise AuthFileModeError(
            f"Error: {path} has loose permissions ({oct(mode)}).\n"
            f"Fix: chmod 0600 {path}\n"
            f"(Required: mode 0600; current: {oct(mode)})"
        )


def read_auth(path: Path = AUTH_JSON) -> dict[str, Any]:
    _validate_auth_file_mode(path)
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text())
    if not isinstance(parsed, dict):
        return {}
    return parsed


def write_auth(payload: dict[str, Any], path: Path = AUTH_JSON) -> None:
    write_atomic_secret(path, json.dumps(payload).encode())


def clear_auth(path: Path = AUTH_JSON) -> None:
    if path.exists():
        path.unlink()
