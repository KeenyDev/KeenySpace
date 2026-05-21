from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from keenyspace_server.ws.import_ import (
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
