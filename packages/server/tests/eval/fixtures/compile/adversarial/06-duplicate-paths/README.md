# Adversarial Fixture 06 — Duplicate Paths

Tests that `CompilePlan.no_duplicate_paths` model validator raises `ValidationError` when
two `PageOp` entries target the same path within a single plan. Duplicate writes within the
same plan are ambiguous (which body wins?) and represent either a model hallucination or a
conflict that requires human resolution. The validator ensures last-writer-wins confusion
never reaches disk.

Covers AI-SPEC §5 Dimension 9 (schema validity) — Critical tier.
