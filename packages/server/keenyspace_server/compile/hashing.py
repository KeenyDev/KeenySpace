from __future__ import annotations

import hashlib
import json

from keenyspace_server.compile.models import CompilePlan


def hash_plan(wal_first_id: str, wal_last_id: str, plan: CompilePlan) -> str:
    """Deterministic sha256 hex over (wal_first_id, wal_last_id, canonical-JSON-of-plan).

    Frontmatter keys are sorted before hashing so dict-insertion-order variation across
    agent retries does not break idempotency. Use `model_dump(mode='json')` +
    `json.dumps(sort_keys=True)` — never `model_dump_json()` which does not sort nested
    `dict[str, Any]` fields.
    """
    plan_dict = plan.model_dump(mode="json")
    for op in plan_dict.get("ops", []):
        op["frontmatter"] = dict(sorted(op.get("frontmatter", {}).items()))
    canonical = json.dumps(plan_dict, sort_keys=True, ensure_ascii=False)
    raw = f"{wal_first_id}|{wal_last_id}|{canonical}"
    return hashlib.sha256(raw.encode()).hexdigest()
