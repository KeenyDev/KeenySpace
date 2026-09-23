"""Token persistence + auth.json mode-0600 invariant.

The CLI refuses to start when auth.json has any group/world bit set on Unix.
KEY_PREFIX mirrors the server's `auth/api_keys.py` so the client can tell
`ks_live_*` API keys from OIDC bearers without a server round-trip.
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
    # A corrupted or truncated auth.json (power-loss mid-write, disk-full
    # during atomic replace, manual edit) must NOT propagate a raw
    # JSONDecodeError stack trace out of every CLI command that touches auth
    # state (status, doctor, ingest, query, lint, compile, backup, restore,
    # daemon/post_compact). Surface a clean error pointing the user at
    # `keenyspace login` to refresh.
    try:
        parsed = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"Error: {path} is unreadable or corrupt ({exc}). Run `keenyspace login` to refresh."
        ) from exc
    if not isinstance(parsed, dict):
        return {}
    return parsed


def write_auth(payload: dict[str, Any], path: Path = AUTH_JSON) -> None:
    write_atomic_secret(path, json.dumps(payload).encode())


def clear_auth(path: Path = AUTH_JSON) -> None:
    if path.exists():
        path.unlink()
