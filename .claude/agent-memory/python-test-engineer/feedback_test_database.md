---
name: feedback-test-database
description: KeenySpace server suite may only run against the keenyspace_test_c database; its fixtures DROP SCHEMA public CASCADE
metadata:
  type: feedback
---

Run the KeenySpace server test suite only against `keenyspace_test_c`
(`KEENYSPACE_DB__URL=postgresql+asyncpg://keenyspace:testpw@localhost:55432/keenyspace_test_c`).
Never point it at a dev, shared, or named workspace database.

**Why:** the suite's own reset helpers (`tests/conftest.py::_reset_schema` and
`tests/integration/conftest.py::_reset_schema`) execute `DROP SCHEMA public CASCADE`
before migrations, and the alembic tests shell out to `uv run alembic upgrade head`
against the same URL. Pointing it elsewhere destroys that database silently.

**How to apply:** whenever running or advising on server tests, pass the
`keenyspace_test_c` URL explicitly; the client suite needs no database at all. The
`real_idp` marker lane needs a live Authentik via testcontainers and is deselected by
default (`addopts = -m 'not real_idp'`).
