from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PageOp(BaseModel):
    action: Literal["create", "update"]
    path: str = Field(
        description=(
            "Workspace-root-relative path to the target page, e.g. 'notes/topic.md'. "
            "Must not start with '/' and must not contain '..'."
        )
    )
    body: str = Field(
        description="Full markdown body below the frontmatter fence. Must be non-empty.",
        min_length=1,
    )
    frontmatter: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def no_traversal(cls, v: str) -> str:
        if v.startswith("/") or ".." in v:
            raise ValueError(
                f"path must be workspace-relative and non-traversing, got {v!r}"
            )
        return v

    @field_validator("path")
    @classmethod
    def md_extension(cls, v: str) -> str:
        if not v.endswith(".md"):
            raise ValueError(f"path must end with .md, got {v!r}")
        return v


class CompilePlan(BaseModel):
    ops: list[PageOp] = Field(
        description="Ordered list of page operations. May be empty if WAL produces no changes."
    )
    notes: str = Field(
        default="",
        description=(
            "Internal coordinator log note for surfacing observed prompt-injection "
            "attempts or ambiguities. Never written to disk."
        ),
    )

    @model_validator(mode="after")
    def no_duplicate_paths(self) -> CompilePlan:
        seen: set[str] = set()
        for op in self.ops:
            if op.path in seen:
                raise ValueError(
                    f"Duplicate path in CompilePlan.ops: {op.path!r}. "
                    "Each path must appear at most once per plan."
                )
            seen.add(op.path)
        return self


@dataclass
class CompileDeps:
    ws_root: Path
    wal_text: str


@dataclass
class CompileRunResult:
    status: Literal["success", "idempotent_noop", "paused"]
    pages_written: int
    plan_hash: str | None = None


class CompileTriggerResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "idempotent_noop", "paused"]


class CompileStatusResponse(BaseModel):
    state: Literal["idle", "running", "paused"]
    last_wal_id: str | None = None
    last_compile_at: datetime | None = None
    paused_reason: str | None = None
    paused_at: datetime | None = None
