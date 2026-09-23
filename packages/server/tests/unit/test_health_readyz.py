"""/readyz is public: failure details stay in the log, not the response body."""

from __future__ import annotations

import json
from typing import Any

import pytest
from keenyspace_server.api.health import readyz

_LEAKY = "connection to postgresql://keenyspace:hunter2@db.internal:5432 failed"


class _BrokenEngine:
    def connect(self) -> Any:
        raise OSError(_LEAKY)


async def test_readyz_hides_postgres_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keenyspace_server.db.session as session_mod

    monkeypatch.setattr(session_mod, "get_engine", lambda: _BrokenEngine())

    resp = await readyz()

    body = json.loads(bytes(resp.body))
    assert resp.status_code == 503
    assert body["checks"]["postgres"] == "error"
    assert "hunter2" not in bytes(resp.body).decode()
    assert "db.internal" not in bytes(resp.body).decode()


async def test_readyz_hides_settings_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keenyspace_server.config as config_mod

    def _explode() -> Any:
        raise RuntimeError(_LEAKY)

    monkeypatch.setattr(config_mod, "get_settings", _explode)

    resp = await readyz()

    body = json.loads(bytes(resp.body))
    assert resp.status_code == 503
    assert body["checks"]["fs_root"] == "error"
    assert "hunter2" not in bytes(resp.body).decode()
