# Domain Fixture 03 — Heading Structure Preservation

Tests that when the compile agent updates a page with an existing heading hierarchy, all
existing headings survive at their original levels in `PageOp.body`. The vault page has a
5-level hierarchy (# → ## → ## → ### → ###). The WAL adds new content under one section.
The synthesized plan body must retain all 5 original headings at their original levels.

Covers AI-SPEC §5 Dimension 7 (heading structure preservation) — High tier.
