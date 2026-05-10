# Adversarial Fixture 04 — Invalid Action Enum

Tests that Pydantic rejects a `PageOp` constructed with `action="archive"` — a value outside
the `Literal["create", "update"]` constraint. The schema validator raises `ValidationError`
before the plan can reach the coordinator. This ensures that model hallucinations about
permitted operations are caught at the boundary.

Covers AI-SPEC §5 Dimension 9 (schema validity) — Critical tier.
