from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AppendLogRequest(BaseModel):
    workspace: str
    content: str
    parent_id: str | None = None


class AppendLogResponse(BaseModel):
    entry_id: str
    ts: datetime


class ReadPageResponse(BaseModel):
    path: str
    content: str
    frontmatter: dict[str, Any]
