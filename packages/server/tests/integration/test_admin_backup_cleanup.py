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
