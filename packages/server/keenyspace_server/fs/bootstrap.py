from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import structlog

from .atomic import write_atomic

log = structlog.get_logger(__name__)

BLUEPRINT_SYNC_MANIFEST = ".image-sync.json"
_MANIFEST_SCHEMA_VERSION = 1


def ensure_fs_root_layout(
    fs_root: Path, server_blueprints_image_dir: Path
) -> None:
    for subdir in ("workspaces", "blueprints", ".tmp"):
        (fs_root / subdir).mkdir(parents=True, exist_ok=True)

    blueprints_root = fs_root / "blueprints"
    default_target = blueprints_root / "default"
    default_src = server_blueprints_image_dir / "default"
    if default_src.exists():
        manifest = _load_manifest(blueprints_root)
        shipped = dict(manifest.get("default", {}))
        if not default_target.exists():
            shutil.copytree(
                default_src,
                default_target,
                symlinks=False,
                dirs_exist_ok=False,
                ignore_dangling_symlinks=True,
            )
            shipped = _digest_tree(default_src)
        else:
            # G-3 (Phase 4 UAT): reconcile the on-disk blueprint catalog with
            # the image on EVERY boot. Files missing on disk are added; files
            # still byte-identical to what the image last shipped are upgraded
            # in place; anything an operator has edited is left alone and
            # reported, so drift is visible instead of silent.
            _merge_blueprint_tree(default_src, default_target, shipped)
        if shipped != manifest.get("default"):
            manifest["default"] = shipped
            _save_manifest(blueprints_root, manifest)

    _sweep_stale_tmp(fs_root / ".tmp")


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest_tree(src: Path) -> dict[str, str]:
    """Map every regular file under ``src`` to its sha256, keyed by rel path."""
    digests: dict[str, str] = {}
    for src_root, dirnames, filenames in os.walk(src, followlinks=False):
        rel_root = Path(src_root).relative_to(src)
        dirnames[:] = [
            d for d in dirnames if not (Path(src_root) / d).is_symlink()
        ]
        for filename in filenames:
            src_file = Path(src_root) / filename
            if src_file.is_symlink():
                continue
            try:
                digests[(rel_root / filename).as_posix()] = _digest_file(src_file)
            except OSError as exc:
                log.warning(
                    "fs.bootstrap.blueprint_digest_failed",
                    path=str(src_file),
                    error=str(exc),
                )
    return digests


def _manifest_path(blueprints_root: Path) -> Path:
    return blueprints_root / BLUEPRINT_SYNC_MANIFEST


def _load_manifest(blueprints_root: Path) -> dict[str, dict[str, str]]:
    """Read the shipped-digest manifest; an unreadable file reads as empty.

    The manifest records what the *image* last shipped for each blueprint file,
    which is what separates "on-disk copy is a stale shipped default" from
    "operator edited this". Losing it is safe: every file then falls back to
    skip-on-exists, the pre-manifest behaviour.
    """
    path = _manifest_path(blueprints_root)
    try:
        raw = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning(
            "fs.bootstrap.blueprint_manifest_unreadable",
            path=str(path),
            error=str(exc),
        )
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        log.warning("fs.bootstrap.blueprint_manifest_invalid", path=str(path))
        return {}
    blueprints = raw.get("blueprints")
    if not isinstance(blueprints, dict):
        return {}
    return {
        name: {k: v for k, v in files.items() if isinstance(v, str)}
        for name, files in blueprints.items()
        if isinstance(files, dict)
    }


def _save_manifest(
    blueprints_root: Path, manifest: dict[str, dict[str, str]]
) -> None:
    payload = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "blueprints": manifest,
    }
    try:
        write_atomic(
            _manifest_path(blueprints_root),
            json.dumps(payload, indent=2, sort_keys=True).encode(),
        )
    except OSError as exc:
        log.warning(
            "fs.bootstrap.blueprint_manifest_write_failed",
            path=str(_manifest_path(blueprints_root)),
            error=str(exc),
        )


def _merge_blueprint_tree(
    src: Path, dst: Path, shipped: dict[str, str] | None = None
) -> None:
    """Reconcile ``dst`` against the image tree ``src``.

    Per file: absent on disk means copy; byte-identical to the digest the image
    last shipped (``shipped``) means the on-disk copy is an untouched default
    and gets upgraded to the current image version; anything else is treated as
    an operator edit and preserved. ``shipped`` is updated in place to the
    digests now on disk. Passing ``shipped=None`` degrades to pure
    skip-on-exists.

    Best-effort: an OSError on an individual file logs a warning and continues
    (same shape as ``_sweep_stale_tmp``). Symlinks in ``src`` are skipped.
    """
    for src_root, dirnames, filenames in os.walk(src, followlinks=False):
        rel_root = Path(src_root).relative_to(src)
        # Skip symlinked sub-directories defence-in-depth (os.walk followlinks=False
        # already refuses to descend, but pruning here avoids touching the entries).
        dirnames[:] = [
            d for d in dirnames if not (Path(src_root) / d).is_symlink()
        ]
        dst_root = dst / rel_root
        try:
            dst_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning(
                "fs.bootstrap.blueprint_merge_mkdir_failed",
                path=str(dst_root),
                error=str(exc),
            )
            continue
        for filename in filenames:
            src_file = Path(src_root) / filename
            dst_file = dst_root / filename
            if src_file.is_symlink():
                continue
            rel_path = (rel_root / filename).as_posix()
            try:
                if not dst_file.exists():
                    shutil.copy2(src_file, dst_file)
                    if shipped is not None:
                        shipped[rel_path] = _digest_file(src_file)
                    continue
                if shipped is None:
                    continue
                src_digest = _digest_file(src_file)
                dst_digest = _digest_file(dst_file)
                if dst_digest == src_digest:
                    shipped[rel_path] = src_digest
                    continue
                if shipped.get(rel_path) != dst_digest:
                    log.warning(
                        "fs.bootstrap.blueprint_merge_kept_local_edit",
                        path=str(dst_file),
                        on_disk_sha256=dst_digest,
                        image_sha256=src_digest,
                    )
                    continue
                shutil.copy2(src_file, dst_file)
                shipped[rel_path] = src_digest
                log.info(
                    "fs.bootstrap.blueprint_merge_upgraded",
                    path=str(dst_file),
                    from_sha256=dst_digest,
                    to_sha256=src_digest,
                )
            except OSError as exc:
                log.warning(
                    "fs.bootstrap.blueprint_merge_copy_failed",
                    src=str(src_file),
                    dst=str(dst_file),
                    error=str(exc),
                )


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
