from __future__ import annotations

from pathlib import Path

import pytest
from keenyspace_server.fs.path_safety import is_compile_writable


@pytest.mark.parametrize("denied", [
    ".keenyspace/config.yaml",
    ".keenyspace/blueprint.yaml",
    "logs/2026-05-10.md",
    "_templates/concept.md",
    "raw/assets/diagram.png",
    "CLAUDE.md",
    "../etc/passwd",
    "/abs/path",
    "",
    "x" * 600,
])
def test_denied_paths_return_false(tmp_path: Path, denied: str) -> None:
    assert is_compile_writable(tmp_path, denied) is False


@pytest.mark.parametrize("allowed", [
    "notes/topic.md",
    "concepts/architecture.md",
    "index.md",
    "deep/nested/page.md",
])
def test_allowed_paths_return_true(tmp_path: Path, allowed: str) -> None:
    assert is_compile_writable(tmp_path, allowed) is True


@pytest.mark.parametrize("denied_mixed_case", [
    "Logs/2026-05-10.md",
    "LOGS/entry.md",
    "Raw/assets/diagram.png",
    "RAW/data.md",
    "_Templates/concept.md",
    "Claude.md",
    "claude.md",
])
def test_case_insensitive_denylist(tmp_path: Path, denied_mixed_case: str) -> None:
    assert is_compile_writable(tmp_path, denied_mixed_case) is False
