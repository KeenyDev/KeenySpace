from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from keenyspace_server.compile.settings import CompileSettings

log = structlog.get_logger(__name__)

_MIN_SECRET_BYTES = 32
_PLACEHOLDER_MARKER = "replace-me"


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = "http://localhost:8000"
    log_level: str = "INFO"


class DBSettings(BaseModel):
    url: str
    pool_size: int = 5
    pool_pre_ping: bool = True


class FSSettings(BaseModel):
    root: Path = Path("/var/lib/keenyspace")
    blueprints_dir: Path | None = None


class WALSettings(BaseModel):
    max_entry_bytes: int = 256 * 1024
    retention_days: int | None = None


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oidc_issuer_url: str
    # Back-channel issuer the server uses to fetch OIDC discovery + JWKS. When
    # unset it equals oidc_issuer_url. Set it separately for split-horizon dev:
    # the host CLI + token `iss` use the public oidc_issuer_url
    # (e.g. http://localhost:9000/...) while the server, living in a container,
    # reaches the IdP at http://authentik:9000/.... JWKS keys are
    # host-independent, so validating a public-issuer token against
    # internally-fetched keys is sound.
    oidc_internal_issuer_url: str | None = None
    oidc_client_id: str
    oidc_client_secret: SecretStr
    oidc_redirect_uri: str
    oidc_post_logout_redirect_uri: str

    @property
    def metadata_issuer_url(self) -> str:
        return self.oidc_internal_issuer_url or self.oidc_issuer_url

    session_secret_key: SecretStr
    cookie_path_ks_at: str = "/v1"
    cookie_path_ks_rt: str = "/v1/api/auth"
    cookie_samesite_ks_at: str = "lax"
    cookie_samesite_ks_rt: str = "strict"
    cookie_secure: bool = True

    # pepper защищает от offline rainbow-table при DB dump
    api_key_pepper: SecretStr

    jwks_ttl_seconds: int = 3600
    jwks_min_retry_interval_seconds: int = 30
    jwks_max_retry_interval_seconds: int = 300

    refresh_threshold_seconds: int = 60

    api_key_last_used_debounce_seconds: int = 300

    multi_worker: bool = False
    required_group: str = ""
    # An empty value never widens admin access to all users: the gate refuses every
    # caller. Empty env values fall back to this default (env_ignore_empty), so disable
    # the admin API via KEENYSPACE_ADMIN_API_ENABLED=0 instead.
    admin_group: str = "keenyspace-admins"
    # API keys carry the owner's groups as last seen in an OIDC token. When set,
    # a key whose owner has not authenticated via OIDC within this many days is
    # refused, bounding how long a group removal in the IdP can go unnoticed.
    api_key_group_snapshot_max_age_days: int | None = Field(default=None, ge=1)

    # Warn instead of refusing to start: the dev compose stack ships
    # "replace-me" fallbacks and must keep booting.
    @model_validator(mode="after")
    def _warn_on_weak_secrets(self) -> AuthSettings:
        secrets = {
            "oidc_client_secret": self.oidc_client_secret,
            "session_secret_key": self.session_secret_key,
            "api_key_pepper": self.api_key_pepper,
        }
        for name, secret in secrets.items():
            value = secret.get_secret_value()
            if _PLACEHOLDER_MARKER in value:
                log.warning("config.secret.placeholder", setting=name)
            elif len(value.encode()) < _MIN_SECRET_BYTES:
                log.warning("config.secret.too_short", setting=name, min_bytes=_MIN_SECRET_BYTES)
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KEENYSPACE_",
        env_nested_delimiter="__",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="forbid",
    )

    server: ServerSettings = ServerSettings()
    db: DBSettings
    fs: FSSettings = FSSettings()
    wal: WALSettings = WALSettings()
    auth: AuthSettings
    compile: CompileSettings = CompileSettings()
    auto_migrate: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
