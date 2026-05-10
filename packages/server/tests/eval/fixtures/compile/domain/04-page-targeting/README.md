# Domain Fixture 04 — Page Targeting (Deepest Specific Match)

Tests that the compile agent targets the DEEPEST specific page that matches the WAL content,
not the shallowest. Three pages exist: notes/auth.md, notes/auth/oauth.md, and
notes/auth/oauth/google.md. The WAL entry is about a Google OAuth detail. The synthesized
plan must target notes/auth/oauth/google.md (deepest match) rather than notes/auth.md
(shallowest).

Covers AI-SPEC §5 Dimension 8 (page targeting) — High tier.
