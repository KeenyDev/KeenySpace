from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    dev_token: str | None = None
    multi_worker: bool = False


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
    auth: AuthSettings = AuthSettings()
    auto_migrate: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
