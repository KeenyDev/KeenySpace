# Adversarial Fixture 05 — Traversal Path

Tests that Pydantic rejects `PageOp.path` values that attempt directory traversal:
`../etc/passwd`, `/abs/path`, `x/../escape.md`. The `no_traversal` field validator on
`PageOp` raises `ValidationError` for any path starting with '/' or containing '..'.
This prevents the compile agent from writing to locations outside the workspace root.

Covers AI-SPEC §5 Dimension 9 (schema validity) + Dimension 10 (denylist enforcement) — Critical tier.
