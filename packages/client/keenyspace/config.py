"""Client settings — D-01 LLM provider config + YAML + env override.

Precedence (highest → lowest):
init kwargs → env vars (KEENYSPACE_*) → ~/.config/keenyspace/config.yaml → defaults.

Achieved via `settings_customise_sources` returning env source ahead of a
YamlConfigSettingsSource; the `__` env-nesting delimiter (e.g.
`KEENYSPACE_LLM__PROVIDER=openai`) wins over the YAML `llm: provider:` value.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from keenyspace.paths import CONFIG_YAML


class LlmSettings(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key_env: str = "ANTHROPIC_API_KEY"
    timeout_seconds: int = 120


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KEENYSPACE_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
        yaml_file=str(CONFIG_YAML),
        yaml_file_encoding="utf-8",
    )

    server_url: str
    default_workspace: str | None = None
    llm: LlmSettings = LlmSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
        )


def load_config_yaml(path: Path = CONFIG_YAML) -> dict[str, Any]:
    if not path.is_file():
        return {}
    parsed = yaml.safe_load(path.read_text())
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


@lru_cache
def get_client_settings() -> ClientSettings:
    return ClientSettings()  # type: ignore[call-arg]
