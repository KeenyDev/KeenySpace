"""pg_dump.sql safety check run before /v1/admin/restore hands a dump to psql."""

from __future__ import annotations

from pathlib import Path

import pytest
from keenyspace_server.api.admin import UnsafeDumpError, _check_dump_safe

_PLAIN_DUMP = b"""--
-- PostgreSQL database dump
--

\\restrict Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA
SET statement_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET default_tablespace = '';
DROP TABLE IF EXISTS public.users;
CREATE TABLE public.users (
    sub character varying(255) NOT NULL,
    display_name text DEFAULT 'it''s -- not a comment; (' NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL /* a /* nested */ ; */
);
COPY public.users (sub, display_name, payload, created_at) FROM stdin;
alice\t\\N\t{"k": "a\\\\b"}\t2026-01-01 00:00:00+00
\\\\! echo not-a-command\tx\\ty\t{}\t2026-01-01 00:00:00+00
\\.
SELECT pg_catalog.setval('public.audit_log_id_seq', 5, true);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (sub);

--
-- PostgreSQL database dump complete
--

\\unrestrict Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA
"""


def _write(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "pg_dump.sql"
    path.write_bytes(content)
    return path


def test_plain_pg_dump_with_copy_data_is_accepted(tmp_path: Path) -> None:
    _check_dump_safe(_write(tmp_path, _PLAIN_DUMP))


def test_copy_block_without_terminator_is_accepted_at_eof(tmp_path: Path) -> None:
    dump = b"COPY public.t (a) FROM stdin;\n\\N\n"

    _check_dump_safe(_write(tmp_path, dump))


@pytest.mark.parametrize(
    ("dump", "reason"),
    [
        pytest.param(b"SELECT 1;\n\\! echo pwned\n", "backslash", id="shell-line"),
        pytest.param(b"SELECT 1; \\! echo pwned\n", "backslash", id="shell-mid-line"),
        pytest.param(b"  \\o /tmp/out\n", "backslash", id="indented-meta"),
        pytest.param(
            b"COPY public.t (a) FROM stdin;\nx\n\\.\n\\! echo pwned\n",
            "backslash",
            id="after-copy-end",
        ),
        pytest.param(
            b"SELECT '\nCOPY public.t (a) FROM stdin;\n'; \\! echo pwned\n\\.\n",
            "backslash",
            id="copy-inside-string",
        ),
        pytest.param(
            b"/*\nCOPY public.t (a) FROM stdin;\n*/ \\! echo pwned\n\\.\n",
            "backslash",
            id="copy-inside-comment",
        ),
        pytest.param(
            b"SELECT (1;\nCOPY public.t (a) FROM stdin;\n) \\! echo pwned\n\\.\n",
            "backslash",
            id="copy-inside-parens",
        ),
        pytest.param(
            b"SELECT 1\nCOPY public.t (a) FROM stdin;\n\\! echo pwned\n\\.\n",
            "backslash",
            id="copy-continuing-statement",
        ),
        pytest.param(
            b'COPY "public.t (a) FROM stdin;\n\\! echo pwned\n\\.\n',
            "backslash",
            id="copy-unbalanced-quote",
        ),
        pytest.param(b"SELECT 1\n\\restrict abc\n", "backslash", id="restrict-mid-statement"),
        pytest.param(b"SELECT $$x$$;\n", "dollar", id="dollar-quote"),
        pytest.param(b"SELECT :foo;\n", "variable", id="psql-variable"),
        pytest.param(b"SELECT :'foo';\n", "variable", id="psql-quoted-variable"),
        pytest.param(
            b"CREATE FUNCTION f() RETURNS int LANGUAGE sql\nBEGIN ATOMIC SELECT 1; END;\n",
            "BEGIN",
            id="begin-atomic",
        ),
        pytest.param(b"SELECT 1;\x00\\! id\n", "NUL", id="nul-byte"),
    ],
)
def test_meta_command_vectors_are_rejected(tmp_path: Path, dump: bytes, reason: str) -> None:
    with pytest.raises(UnsafeDumpError, match=reason):
        _check_dump_safe(_write(tmp_path, dump))


def test_typecasts_are_not_mistaken_for_variables(tmp_path: Path) -> None:
    _check_dump_safe(_write(tmp_path, b"SELECT '1'::int, now()::date;\n"))
