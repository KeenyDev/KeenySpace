from __future__ import annotations

import pytest
from pydantic import ValidationError


def _set_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave 0 introduced required AuthSettings fields (OIDC + pepper + session secret).
    Tests that build Settings() must populate the full contract."""
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_ISSUER_URL",
        "http://localhost:9999/application/o/test/",
    )
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_CLIENT_ID", "test-client")
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_REDIRECT_URI",
        "http://localhost:8000/v1/api/auth/callback",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI",
        "http://localhost:8000/",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__API_KEY_PEPPER",
        "test-pepper-32chars-padded-here!",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__SESSION_SECRET_KEY",
        "test-session-secret-32chars-pad!",
    )


def test_get_settings_from_env(monkeypatch):
    monkeypatch.setenv("KEENYSPACE_DB__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("KEENYSPACE_FS__ROOT", "/tmp/k")
    _set_auth_env(monkeypatch)

    import keenyspace_server.config as cfg

    cfg.get_settings.cache_clear()

    try:
        s = cfg.get_settings()
        assert str(s.db.url) == "postgresql+asyncpg://u:p@h/d"
        assert str(s.fs.root) == "/tmp/k"
        assert s.wal.max_entry_bytes == 256 * 1024
        assert s.auth.multi_worker is False
    finally:
        cfg.get_settings.cache_clear()


def test_missing_db_url_raises(monkeypatch):
    monkeypatch.delenv("KEENYSPACE_DB__URL", raising=False)

    import keenyspace_server.config as cfg

    cfg.get_settings.cache_clear()

    try:
        with pytest.raises((ValidationError, Exception)):
            cfg.get_settings()
    finally:
        cfg.get_settings.cache_clear()


def test_auto_migrate_default_false(monkeypatch):
    monkeypatch.setenv("KEENYSPACE_DB__URL", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
    monkeypatch.delenv("KEENYSPACE_AUTO_MIGRATE", raising=False)
    _set_auth_env(monkeypatch)

    import keenyspace_server.config as cfg

    cfg.get_settings.cache_clear()

    try:
        s = cfg.get_settings()
        assert s.auto_migrate is False
    finally:
        cfg.get_settings.cache_clear()


def test_auto_migrate_true(monkeypatch):
    monkeypatch.setenv("KEENYSPACE_DB__URL", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("KEENYSPACE_AUTO_MIGRATE", "true")
    _set_auth_env(monkeypatch)

    import keenyspace_server.config as cfg

    cfg.get_settings.cache_clear()

    try:
        s = cfg.get_settings()
        assert s.auto_migrate is True
    finally:
        cfg.get_settings.cache_clear()


def test_auto_migrate_false_string(monkeypatch):
    monkeypatch.setenv("KEENYSPACE_DB__URL", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("KEENYSPACE_AUTO_MIGRATE", "false")
    _set_auth_env(monkeypatch)

    import keenyspace_server.config as cfg

    cfg.get_settings.cache_clear()

    try:
        s = cfg.get_settings()
        assert s.auto_migrate is False
    finally:
        cfg.get_settings.cache_clear()


def test_auto_migrate_truthy_one(monkeypatch):
    monkeypatch.setenv("KEENYSPACE_DB__URL", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("KEENYSPACE_AUTO_MIGRATE", "1")
    _set_auth_env(monkeypatch)

    import keenyspace_server.config as cfg

    cfg.get_settings.cache_clear()

    try:
        s = cfg.get_settings()
        assert s.auto_migrate is True
    finally:
        cfg.get_settings.cache_clear()
