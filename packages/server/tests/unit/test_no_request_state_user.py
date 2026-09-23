"""Regression guard: production source must not read request.state.user.

Starlette AuthMiddleware populates request.user (BaseUser via
conn.scope["user"]). The three workspace endpoints (archive/export/import)
initially read request.state.user, which yielded HTTP 500 (AttributeError)
because nothing populates state.user. This grep guard prevents reintroduction.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

_FORBIDDEN = re.compile(r"\brequest\s*\.\s*state\s*\.\s*user\b")
_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "keenyspace_server"


def _iter_py_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        if any(part == "__pycache__" for part in path.parts):
            continue
        yield path


def test_production_source_does_not_use_request_state_user() -> None:
    """Banned identifier sweep across packages/server/keenyspace_server/**.py."""
    offenders: list[tuple[str, int, str]] = []
    for path in _iter_py_files(_PACKAGE_ROOT):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FORBIDDEN.search(line):
                offenders.append((str(path), lineno, line.strip()))
    assert not offenders, (
        "request.state.user is forbidden in production source - use "
        "request.user (BaseUser populated by Starlette AuthMiddleware). "
        "Offenders:\n"
        + "\n".join(f"  {p}:{ln}: {src}" for p, ln, src in offenders)
    )


def test_grep_guard_self_check_detects_bad_pattern(tmp_path: Path) -> None:
    """Self-test for the regex: a fake file with request.state.user must match."""
    sample = tmp_path / "bad.py"
    sample.write_text("def f(request): return request.state.user\n", encoding="utf-8")
    assert _FORBIDDEN.search(sample.read_text(encoding="utf-8")) is not None
