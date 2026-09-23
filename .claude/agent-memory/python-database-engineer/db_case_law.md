---
name: db-case-law
description: KeenySpace DB case law - alembic single-transaction runs, restore via per-table pg_dump --clean, JSON vs JSONB and none_as_null quirks, compile intent design (0005)
metadata:
  type: project
---

Verified 2026-09-22 (migration 0005). Re-check against current code before reuse.

- Alembic env.py runs ALL pending revisions in ONE transaction (no transaction_per_migration). NOT VALID + VALIDATE inside a migration gives no lock benefit; migrations run at boot (auto_migrate) before the single uvicorn worker serves. **How to apply:** preflight SELECT counts + clear RuntimeError before DDL; SET LOCAL lock_timeout.
- Backup/restore (api/admin.py) = pg_dump --clean --if-exists per table in PG_TABLES_DUMPED; restore refuses mismatched alembic_head unless --force. A NEW table with an FK to workspaces would break a --force restore of an older dump (DROP TABLE workspaces blocked, and re-upgrade hits "table exists"). **How to apply:** prefer new columns on dumped tables over new tables; if a table is added, update PG_TABLES_DUMPED and PG_TABLES_FK_ORDER.
- SQLAlchemy JSON binds Python None as JSON 'null', not SQL NULL - use JSON(none_as_null=True). A CHECK constraint caught this.
- JSONB reorders object keys; compile page frontmatter order is agent-decided (D-06), so stored plans use JSON.
- compile_cursors (0005): last_wal_id/last_compile_hash nullable (paired CHECK) so a first pass can record a pending intent (pending_wal_last_id, pending_plan_hash, pending_plan all-or-nothing CHECK). Advance = CAS on last_wal_id IS NOT DISTINCT FROM expected AND pending_plan_hash.
- `sessions` table is unused by server code and not in PG_TABLES_DUMPED; left in place pending a contract release.
