"""Subprocess plumbing shared by admin backup (pg_dump) and restore (psql)."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from keenyspace_server.api import admin

_CHATTY = (
    "import sys; sys.stderr.write('e' * 300_000); sys.stderr.flush(); "
    "sys.stdout.write('o' * 300_000)"
)


async def test_large_stderr_and_stdout_do_not_deadlock() -> None:
    received = bytearray()

    async def _drain(stdout: asyncio.StreamReader) -> None:
        received.extend(await stdout.read())

    returncode, stderr = await asyncio.wait_for(
        admin._run_pg_client(
            [sys.executable, "-c", _CHATTY], dict(os.environ), drain_stdout=_drain
        ),
        timeout=30,
    )

    assert returncode == 0
    assert len(stderr) == 300_000
    assert len(received) == 300_000


async def test_undrained_stdout_goes_to_devnull() -> None:
    returncode, stderr = await asyncio.wait_for(
        admin._run_pg_client([sys.executable, "-c", _CHATTY], dict(os.environ)),
        timeout=30,
    )

    assert returncode == 0
    assert len(stderr) == 300_000


async def test_timeout_kills_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "PG_CLIENT_TIMEOUT_S", 0.5)

    with pytest.raises(TimeoutError):
        await admin._run_pg_client(
            [sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ)
        )


def test_pg_env_forwards_only_path_and_libpq_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("KEENYSPACE_AUTH__API_KEY_PEPPER", "pepper")
    monkeypatch.setenv("PGSSLMODE", "require")

    env = admin._pg_env("postgresql+asyncpg://ks:secretpw@db:5432/ks", lock_timeout_ms=1000)

    assert all(key == "PATH" or key.startswith("PG") for key in env), sorted(env)
    assert env["PGPASSWORD"] == "secretpw"
    assert env["PGSSLMODE"] == "require"
    assert env["PGOPTIONS"] == "-c lock_timeout=1000"
    assert "ANTHROPIC_API_KEY" not in env
    assert "KEENYSPACE_AUTH__API_KEY_PEPPER" not in env


def test_psql_argv_ignores_psqlrc() -> None:
    argv = admin._psql_argv("postgresql+asyncpg://ks@db/ks")

    assert "--no-psqlrc" in argv
    assert "--single-transaction" in argv


def test_percent_encoded_credentials_are_decoded_for_libpq() -> None:
    url = "postgresql+asyncpg://ks%40ops:p%40ss%2Fw%3Ard%25@db.internal:5433/keenyspace"

    env = admin._pg_env(url)
    dump_argv = admin._pg_dump_argv(url)
    psql_argv = admin._psql_argv(url)

    assert env["PGPASSWORD"] == "p@ss/w:rd%"
    assert dump_argv[dump_argv.index("-U") + 1] == "ks@ops"
    assert psql_argv[psql_argv.index("-U") + 1] == "ks@ops"
