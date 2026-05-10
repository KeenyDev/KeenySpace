# Domain Fixture 02 — Wikilink Hygiene

Tests that the compile agent only produces `[[wikilinks]]` that resolve to either existing
vault pages or other `PageOp.path` entries in the same plan. The WAL mentions `[[auth]]`
(resolves to vault/notes/auth.md) and `[[unknown-page]]` (no such file). The synthesized
plan body must include `[[auth]]` and must NOT include `[[unknown-page]]` (unresolvable).

Covers AI-SPEC §5 Dimension 5 (wikilink hygiene) — High tier.
