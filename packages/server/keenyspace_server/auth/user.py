from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from starlette.authentication import BaseUser


@dataclass
class User(BaseUser):
    sub: str
    _display_name: str
    source: Literal["oidc", "api_key"]
    groups: list[str] = field(default_factory=list)
    # When the IdP asserted `groups`: the token's iat for an OIDC token carrying a
    # groups claim (or the stored snapshot's time when that is newer than the
    # token), the owner's snapshot time for an API key, None when unknown.
    groups_seen_at: datetime | None = None
    # OIDC token iat; None for API keys.
    issued_at: datetime | None = None

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def identity(self) -> str:
        return self.sub
