# Adversarial Fixture 03 — Tool-Call Loop

Tests that a FunctionModel that always returns the same `read_page("notes/index.md")` tool
call triggers the `LoopDetector` after max_repeats identical calls and raises
`UsageLimitExceeded`. This ensures the compile coordinator transitions to
`compile_state='paused'` with `reason='loop_abort'` rather than running unboundedly.

Covers AI-SPEC §5 Dimension 2 (loop-abort) — Critical tier.
