"""Admin backup scratch never outlives the request that created it."""

from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PG_URL = os.environ.get("KEENYSPACE_DB__URL")
HAS_PG_DUMP = shutil.which("pg_dump") is not None

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
    pytest.mark.skipif(not HAS_PG_DUMP, reason="pg_dump binary unavailable"),
]


async def test_backup_response_never_iterated_leaves_no_scratch(
    app: Any, api_key_user: tuple[str, str], fs_root: Path
) -> None:
    from keenyspace_server.api.admin import admin_backup
    from keenyspace_server.db.session import get_db_session

    user_sub, _ = api_key_user
    request = SimpleNamespace(user=SimpleNamespace(sub=user_sub), app=app)

    async with get_db_session() as session:
        response = await admin_backup(request, session)  # type: ignore[arg-type]  # duck-typed Request stub

    assert list((fs_root / "tmp").iterdir()) == []

    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]  # Starlette types it as a union of sync/async iterables
    assert len(body) == int(response.headers["content-length"])
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        assert tar.getnames()[0] == "manifest.json"


async def test_backup_holds_no_transaction_while_pg_dump_runs(
    app: Any, api_key_user: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from keenyspace_server.api import admin
    from keenyspace_server.db.session import get_db_session

    user_sub, _ = api_key_user
    request = SimpleNamespace(user=SimpleNamespace(sub=user_sub), app=app)
    real_pg_dump = admin._run_pg_dump
    in_transaction_during_dump: list[bool] = []

    async with get_db_session() as session:

        async def _observing_pg_dump(db_url: str, out_path: Path) -> None:
            in_transaction_during_dump.append(session.in_transaction())
            await real_pg_dump(db_url, out_path)

        monkeypatch.setattr(admin, "_run_pg_dump", _observing_pg_dump)
        response = await admin.admin_backup(request, session)  # type: ignore[arg-type]  # duck-typed Request stub
        async for _ in response.body_iterator:  # type: ignore[union-attr]  # async generator at runtime
            pass

    assert in_transaction_during_dump == [False]
