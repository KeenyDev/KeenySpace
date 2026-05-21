from __future__ import annotations

import shutil
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


def ensure_fs_root_layout(
    fs_root: Path, server_blueprints_image_dir: Path
) -> None:
    for subdir in ("workspaces", "blueprints", ".tmp"):
        (fs_root / subdir).mkdir(parents=True, exist_ok=True)

    default_target = fs_root / "blueprints" / "default"
    if not default_target.exists():
        default_src = server_blueprints_image_dir / "default"
        if default_src.exists():
            shutil.copytree(
                default_src,
                default_target,
                symlinks=False,
                dirs_exist_ok=False,
                ignore_dangling_symlinks=True,
            )

    _sweep_stale_tmp(fs_root / ".tmp")


def _sweep_stale_tmp(tmp_root: Path) -> None:
    """Reap stale ``import_*`` / ``upload_*`` entries left by killed requests.

    WR-14: the in-request ``finally`` blocks in ``api/workspace_import.py``
    and ``ws/import_.py`` only run if the worker survives long enough to
    execute them. ``kill -9``, OOM-killer, container restart, or a stuck
    ``await file.read(...)`` mid-cancellation leave staged extractions and
    partial uploads on disk indefinitely. v1 ships single-worker uvicorn,
    so at startup no other process is mid-import; a sweep here is safe.

    Best-effort: failures to remove an entry log a warning and continue
    (a stuck mount or permission issue should not block server boot).
    """
    if not tmp_root.is_dir():
        return
    for entry in tmp_root.iterdir():
        if not entry.name.startswith(("import_", "upload_")):
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "fs.startup.tmp_cleanup_failed",
                path=str(entry),
                error=str(exc),
            )
