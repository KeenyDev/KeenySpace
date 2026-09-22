from __future__ import annotations

from pathlib import Path

import yaml

from keenyspace_server.compile.models import CompilePlan
from keenyspace_server.fs.atomic import write_atomic
from keenyspace_server.fs.path_safety import is_compile_writable, validate_relative_path


class CompilePlanSafetyError(Exception):
    """Raised when a CompilePlan contains a PageOp.path on the denylist (D-07)."""

    def __init__(self, path: str) -> None:
        super().__init__(f"PageOp.path {path!r} violates compile denylist")
        self.path = path


def _serialize_page(frontmatter: dict[str, object], body: str) -> bytes:
    """frontmatter (preserve agent-decided key order) + body. PyYAML sort_keys=False per D-06.

    IMPORTANT: sort_keys=False is REQUIRED here. The SORTED form is reserved for the
    idempotency hash in compile/hashing.py — never for on-disk frontmatter.
    """
    if frontmatter:
        fm_yaml = yaml.dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        return f"---\n{fm_yaml}---\n{body}".encode()
    return body.encode()


def apply_plan(ws_root: Path, plan: CompilePlan) -> int:
    """Validate every PageOp.path against the denylist, then atomically write all pages.

    Atomicity contract: if ANY PageOp fails the denylist check, NO file is written.
    Returns count of pages written. Coordinator records this as compile_runs.pages_written.
    """
    for op in plan.ops:
        if not is_compile_writable(ws_root, op.path):
            raise CompilePlanSafetyError(op.path)

    for op in plan.ops:
        target = ws_root / validate_relative_path(op.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _serialize_page(op.frontmatter, op.body)
        write_atomic(target, data)

    return len(plan.ops)
