# Client test layout

This suite is deliberately flat: `tests/test_<area>.py`, one module per CLI command,
daemon component, or config surface (`test_pull_dirty.py`, `test_daemon_killswitch.py`,
`test_config.py`). The client has no database and never builds the server app, so the
server's `unit/` vs `integration/` split would not partition anything here — every test
runs offline against `tmp_path`, an isolated XDG home, a `pytest-httpserver` stub, or a
local unix socket. Put a new test in the module that owns its area, and add a new
`test_<area>.py` when no module does.

Two things are not test modules and must not be named like one:

- `conftest.py` — the shared fixtures (`temp_config_dir`, `short_xdg_state`,
  `cli_runner`, `mock_daemon`, `function_model_agent`). Anything with setup/teardown or
  used by more than one module belongs here.
- `helpers.py` — plain builders (`build_auth_json`, `build_config_yaml`,
  `make_envelope`) with no fixture semantics. Import them explicitly:
  `from tests.helpers import build_auth_json`.

`eval/fixtures/` holds recorded transcript data for post-compact evaluation; `fixtures/`
is a placeholder for the same kind of static data. Both are data, not tests.
