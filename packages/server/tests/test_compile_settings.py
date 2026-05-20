from __future__ import annotations

import os
from unittest.mock import patch

from keenyspace_server.compile.settings import CompileSettings
from keenyspace_server.config import Settings


def test_compile_settings_defaults_match_d09() -> None:
    cs = CompileSettings()
    assert cs.debounce_seconds == 30
    assert cs.backstop_interval_minutes == 15
    assert cs.max_tool_calls == 20
    assert cs.max_input_tokens == 50_000
    assert cs.max_output_tokens == 20_000
    assert cs.max_seconds == 180
    assert cs.daily_token_ceiling == 500_000
    assert cs.model == "claude-sonnet-4-6"
    assert not hasattr(cs, "temperature")


def test_settings_compile_field_loads_env() -> None:
    env = {
        "KEENYSPACE_DB__URL": "postgresql+asyncpg://x:x@localhost/x",
        "KEENYSPACE_COMPILE__MODEL": "claude-opus-4-7",
        "KEENYSPACE_COMPILE__MAX_TOOL_CALLS": "10",
        # Wave 0 AuthSettings required fields.
        "KEENYSPACE_AUTH__OIDC_ISSUER_URL": "http://localhost:9999/application/o/test/",
        "KEENYSPACE_AUTH__OIDC_CLIENT_ID": "test-client",
        "KEENYSPACE_AUTH__OIDC_CLIENT_SECRET": "test-secret",
        "KEENYSPACE_AUTH__OIDC_REDIRECT_URI": "http://localhost:8000/v1/api/auth/callback",
        "KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI": "http://localhost:8000/",
        "KEENYSPACE_AUTH__API_KEY_PEPPER": "test-pepper-32chars-padded-here!",
        "KEENYSPACE_AUTH__SESSION_SECRET_KEY": "test-session-secret-32chars-pad!",
    }
    with patch.dict(os.environ, env, clear=True):
        s = Settings()  # type: ignore[call-arg]
        assert s.compile.model == "claude-opus-4-7"
        assert s.compile.max_tool_calls == 10
        assert s.compile.daily_token_ceiling == 500_000
