from __future__ import annotations


def test_wal_archived_error_class_exists() -> None:
    from keenyspace_server.wal.writer import WorkspaceArchivedError

    assert issubclass(WorkspaceArchivedError, ValueError)
    assert issubclass(WorkspaceArchivedError, Exception)
