# Domain Fixture 01 — Frontmatter Preservation

Tests that when the compile agent updates an existing page, all existing frontmatter keys
survive in the resulting `PageOp.frontmatter`. The vault page has rich frontmatter with 5 keys
(title, status, owner, created, tags). The WAL entry appends new content. The synthesized plan
must carry forward all 5 existing keys to satisfy the D-06 full-overwrite policy: since the
agent writes the complete page, it must explicitly preserve frontmatter it read.

Covers AI-SPEC §5 Dimension 6 (frontmatter preservation) — High tier.
