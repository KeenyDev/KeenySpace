"""Pitfall #1 enforcement: `keenyspace --help` cold-boot must stay under 600ms.

Design target is 300ms; the 600ms threshold absorbs pytest+subprocess
cold-start variance but still catches >1s regressions (e.g. accidental
top-level pydantic-ai import).
"""

from __future__ import annotations

import subprocess
import sys
import time


def test_cli_help_under_600ms() -> None:
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "keenyspace", "--help"],
        capture_output=True,
        check=True,
        timeout=10,
    )
    elapsed_ms = (time.monotonic() - start) * 1000.0
    assert b"keenyspace" in result.stdout.lower() or b"Usage" in result.stdout
    assert elapsed_ms < 600, f"Cold-boot {elapsed_ms:.0f}ms exceeded 600ms budget"
