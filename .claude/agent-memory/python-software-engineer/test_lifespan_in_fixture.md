---
name: test-lifespan-in-fixture
description: KeenySpace server tests cannot run app.router.lifespan_context inside a pytest-asyncio yield fixture (anyio cancel-scope error on teardown); use an in-test asynccontextmanager
metadata:
  type: feedback
---

Running the full FastAPI app lifespan (`app.router.lifespan_context(app)`) from a pytest-asyncio yield fixture passes the test body but errors on teardown with anyio "Attempted to exit cancel scope in a different task than it was entered in" (the FastMCP lifespan task group is entered in the setup task and exited in the teardown task).

**Why:** pytest-asyncio runs fixture setup and teardown in different tasks; FastMCP's lifespan uses an anyio task group.

**How to apply:** when a test needs the real lifespan (fs bootstrap, blueprints, coordinator), wrap it in a module-local `@contextlib.asynccontextmanager` helper and `async with` it inside each test body (pattern in tests/test_append_log_rest_errors.py and tests/test_compile_coordinator_passes.py). The conftest `client` fixture only runs `engine_lifespan` (no fs bootstrap), so workspace creation through it depends on blueprint availability.
