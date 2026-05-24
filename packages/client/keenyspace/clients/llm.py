"""Server-driven pydantic-ai Agent runner.

Phase 5 D-02/D-04/D-05: a CLI command pulls Instructions from the server
(prompt + tool_whitelist + budgets), then runs a pydantic-ai Agent against
the configured provider (Anthropic default). The agent's MCP toolset is
exposed via MCPServerStreamableHTTP — `process_tool_call` enforces the
tool_whitelist client-side (Pitfall #7 defence-in-depth).

Budget triple-guard mirrors the Phase 2 compile pattern:
  - UsageLimits (request_limit + total_tokens_limit)
  - asyncio.wait_for (wall-clock seconds)
  - LoopDetector capability (same tool+args_hash 3x → ModelRetry → abort)

All three failure modes surface as `BudgetAbort` with a stable reason
string (`usage_limit_exceeded` | `timeout_exceeded` | `loop_detected`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from keenyspace_shared.mcp_contracts import Instructions
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.mcp import MCPServerStreamableHTTP

from keenyspace.agent.budgets import BudgetAbort, make_loop_detector, make_usage_limits
from keenyspace.agent.tool_filter import (
    ToolWhitelistViolation,
    make_process_tool_call,
)


async def run_server_driven_command(
    *,
    server_url: str,
    api_key: str,
    instructions: Instructions,
    user_prompt: str,
    llm_model: str,
    output_type: type | None = None,
    _agent_factory: Any | None = None,
) -> Any:
    """Run a pydantic-ai Agent driven by server-supplied Instructions.

    ``_agent_factory`` is an injection seam for tests: callers can pass a
    factory taking (model, system_prompt, toolsets) and returning a configured
    Agent. Production callers leave it None.
    """

    base = server_url.rstrip("/")
    process_hook = make_process_tool_call(instructions.tool_whitelist)
    mcp_server = MCPServerStreamableHTTP(
        url=f"{base}/v1/mcp/",
        headers={"Authorization": f"Bearer {api_key}"},
        process_tool_call=process_hook,
    )

    detector = make_loop_detector()
    if _agent_factory is not None:
        agent: Agent = _agent_factory(
            model=llm_model,
            system_prompt=instructions.prompt,
            toolsets=[mcp_server],
        )
    else:
        agent_kwargs: dict[str, Any] = {
            "model": llm_model,
            "toolsets": [mcp_server],
            "instructions": instructions.prompt,
        }
        if output_type is not None:
            agent_kwargs["output_type"] = output_type
        agent = Agent(**agent_kwargs)

    try:
        result = await asyncio.wait_for(
            agent.run(
                user_prompt,
                usage_limits=make_usage_limits(instructions.budgets),
                capabilities=[detector],
            ),
            timeout=float(instructions.budgets.max_seconds),
        )
    except TimeoutError as exc:
        raise BudgetAbort("timeout_exceeded") from exc
    except UsageLimitExceeded as exc:
        if detector.triggered:
            raise BudgetAbort("loop_detected") from exc
        raise BudgetAbort("usage_limit_exceeded") from exc
    except UnexpectedModelBehavior as exc:
        # LoopDetector raises ModelRetry, which exhausts tool max_retries
        # and surfaces as UnexpectedModelBehavior — translate to loop_detected.
        if detector.triggered:
            raise BudgetAbort("loop_detected") from exc
        raise
    except ToolWhitelistViolation as exc:
        raise BudgetAbort(f"tool_whitelist_violation:{exc.tool_name}") from exc
    return result.output
