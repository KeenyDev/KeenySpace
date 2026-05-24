"""Phase 5 D-05: budget enforcement on client-side pydantic-ai agents.

UsageLimits (request + total tokens) + asyncio.wait_for (wall clock) +
LoopDetector (same tool+args 3x). BudgetAbort is the unified abort surface
that CLI commands translate to exit code 3.
"""

from __future__ import annotations

import structlog
from keenyspace_shared.loop_detector import LoopDetector
from keenyspace_shared.mcp_contracts import Budgets
from pydantic_ai.usage import UsageLimits

log = structlog.get_logger(__name__)


class BudgetAbort(Exception):  # noqa: N818 — name dictated by plan; "Abort" reads better than "Error"
    def __init__(
        self,
        reason: str,
        used_steps: int | None = None,
        used_tokens: int | None = None,
    ) -> None:
        self.reason = reason
        self.used_steps = used_steps
        self.used_tokens = used_tokens
        super().__init__(reason)


def make_usage_limits(budgets: Budgets) -> UsageLimits:
    return UsageLimits(
        request_limit=budgets.max_steps,
        total_tokens_limit=budgets.max_tokens,
    )


def make_loop_detector() -> LoopDetector:
    return LoopDetector(max_repeats=3)


def log_aborted(reason: str, command: str, workspace: str | None = None) -> None:
    log.warning(
        "client.agent.aborted",
        reason=reason,
        command=command,
        workspace=workspace,
    )
