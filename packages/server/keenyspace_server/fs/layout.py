from __future__ import annotations

from pathlib import Path
from uuid import UUID


def workspace_root(fs_root: Path, ws_uuid: UUID | str) -> Path:
    """Return the on-disk directory holding workspace ``ws_uuid``.

    ``fs_root`` is ``settings.fs.root``. The directory is neither created nor
    checked here; reads and writes of untrusted relative paths inside it go
    through ``fs.path_safety``.
    """
    return fs_root / "workspaces" / str(ws_uuid)
