from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from keenyspace_server.ws.export import EXPORT_SKIP_TOP_LEVEL
from keenyspace_server.ws.import_ import (
    IMPORT_REJECT_TOP_LEVEL_DENYLIST,
    IMPORT_REJECT_TOP_LEVEL_USER_STATE,
    WorkspaceImportError,
    _validate_zip_sync,
    validate_import_zip,
)


def _make_zip(tmp_path: Path, entries: list[tuple[str, bytes]]) -> Path:
    path = tmp_path / "import.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path


def _make_zip_with_symlink(tmp_path: Path) -> Path:
    path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("evil-link")
        info.external_attr = (0o120777 & 0xFFFF) << 16
        zf.writestr(info, b"/etc/passwd")
        zf.writestr("index.md", b"# ok\n")
    return path


def test_validate_rejects_path_traversal(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path, [("../../../etc/passwd", b"x"), ("index.md", b"# x")])
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "path_traversal"


def test_validate_rejects_absolute_path(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path, [("/etc/passwd", b"x"), ("index.md", b"# x")])
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "path_traversal"


def test_validate_rejects_symlink(tmp_path: Path) -> None:
    zp = _make_zip_with_symlink(tmp_path)
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "symlink"


def test_validate_rejects_no_md(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path, [("raw/img.png", b"\x89PNG")])
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "empty_workspace"


def test_validate_rejects_bad_zip(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.zip"
    bogus.write_bytes(b"not a zip file")
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(bogus)
    assert exc.value.code == "bad_zip"


def test_validate_rejects_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "keenyspace_server.ws.import_.MAX_IMPORT_UNCOMPRESSED_BYTES", 4
    )
    zp = _make_zip(tmp_path, [("index.md", b"hello, world\n")])
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "size_cap"


def test_validate_extracts_preserved_blueprint_ref(tmp_path: Path) -> None:
    cfg = b"uuid: orig\nslug: orig\nblueprint: custom-bp@v0.2\n"
    zp = _make_zip(tmp_path, [(".keenyspace/config.yaml", cfg), ("index.md", b"# x")])
    result = _validate_zip_sync(zp)
    assert result.preserved_blueprint_ref == "custom-bp@v0.2"


def test_validate_returns_default_blueprint_when_config_missing(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path, [("index.md", b"# x")])
    result = _validate_zip_sync(zp)
    assert result.preserved_blueprint_ref is None


@pytest.mark.asyncio
async def test_validate_import_zip_async_wrapper(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path, [("index.md", b"# ok")])
    result = await validate_import_zip(zp)
    assert result.total_bytes == 4


# --- G-4 symmetric top-level dotfile policy --------------------------------


def test_validate_accepts_nested_gitkeep(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path, [("raw/.gitkeep", b""), ("index.md", b"# x")])
    result = _validate_zip_sync(zp)
    assert result.total_bytes >= 3


def test_validate_accepts_nested_dotfile_in_any_subdir(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path,
        [("_templates/.editorconfig", b"root = true\n"), ("index.md", b"# x")],
    )
    result = _validate_zip_sync(zp)
    assert result.total_bytes > 0


def test_validate_accepts_keenyspace_subtree(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path,
        [
            (
                ".keenyspace/config.yaml",
                b"uuid: x\nslug: y\nblueprint: default@v0.1\n",
            ),
            (".keenyspace/instructions/ingest.md", b"---\nname: ingest\n---\n"),
            ("index.md", b"# x"),
        ],
    )
    result = _validate_zip_sync(zp)
    assert result.preserved_blueprint_ref == "default@v0.1"


def test_validate_rejects_top_level_obsidian(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path,
        [(".obsidian/workspace.json", b"{}"), ("index.md", b"# x")],
    )
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "hidden_entry"


def test_validate_rejects_top_level_logs(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path, [("logs/.gitkeep", b""), ("index.md", b"# x")])
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "hidden_entry"


def test_validate_rejects_top_level_git(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path,
        [(".git/HEAD", b"ref: refs/heads/main\n"), ("index.md", b"# x")],
    )
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "hidden_entry"


def test_validate_rejects_top_level_env(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path,
        [(".env", b"SECRET=hunter2\n"), ("index.md", b"# x")],
    )
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "hidden_entry"


def test_validate_rejects_top_level_ds_store(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path, [(".DS_Store", b"Bud1"), ("index.md", b"# x")])
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "hidden_entry"


def test_validate_constant_matches_export() -> None:
    """G-4 symmetry pin: import user-state reject set IS the export skip set."""
    assert IMPORT_REJECT_TOP_LEVEL_USER_STATE == EXPORT_SKIP_TOP_LEVEL


def test_validate_denylist_covers_operator_smuggle_names() -> None:
    """G-4 denylist pin: top-level operator-smuggle names are rejected."""
    assert {".git", ".env", ".envrc", ".htaccess", ".ssh", ".aws", ".DS_Store"} <= set(
        IMPORT_REJECT_TOP_LEVEL_DENYLIST
    )


def test_validate_still_rejects_path_traversal_after_relax(tmp_path: Path) -> None:
    zp = _make_zip(
        tmp_path, [("../etc/passwd", b"x"), ("index.md", b"# x")]
    )
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "path_traversal"


def test_validate_still_rejects_symlink_after_relax(tmp_path: Path) -> None:
    zp = _make_zip_with_symlink(tmp_path)
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "symlink"


def test_validate_rejects_size_cap_without_decompressing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "keenyspace_server.ws.import_.MAX_IMPORT_UNCOMPRESSED_BYTES", 4
    )

    def _fail_testzip(self: zipfile.ZipFile) -> str | None:
        raise AssertionError("testzip must not run before the size cap check")

    monkeypatch.setattr(zipfile.ZipFile, "testzip", _fail_testzip)
    zp = _make_zip(tmp_path, [("index.md", b"hello, world\n")])
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "size_cap"


def test_validate_rejects_crc_mismatch(tmp_path: Path) -> None:
    payload = b"# index\n" + b"x" * 64
    zp = tmp_path / "crc.zip"
    with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("index.md", payload)
    raw = bytearray(zp.read_bytes())
    offset = raw.find(payload)
    raw[offset + len(payload) - 1] ^= 0xFF
    zp.write_bytes(bytes(raw))
    with pytest.raises(WorkspaceImportError) as exc:
        _validate_zip_sync(zp)
    assert exc.value.code == "bad_zip"
