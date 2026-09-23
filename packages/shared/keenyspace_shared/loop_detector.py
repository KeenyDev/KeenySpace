from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import ModelRetry, RunContext, ToolDefinition
from pydantic_ai.capabilities import AbstractCapability, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart


@dataclass
class LoopDetector(AbstractCapability[Any]):
    """Aborts an agent run when (tool, args_hash) repeats max_repeats times.

    The same (tool_name, sha256(args)) pair repeated max_repeats times raises
    ModelRetry. When pydantic-ai exhausts its retry budget it raises
    UsageLimitExceeded; the caller translates that to a loop-abort outcome.

    CRITICAL: instantiate per agent.run() call. Sharing across runs accumulates
    _call_counts and produces false loop detection.
    """

    max_repeats: int = 3
    _call_counts: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    triggered: bool = False

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: WrapToolExecuteHandler,
    ) -> Any:
        args_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        key = (call.tool_name, args_hash)
        self._call_counts[key] += 1
        if self._call_counts[key] >= self.max_repeats:
            self.triggered = True
            raise ModelRetry(
                f"Loop detected: tool {call.tool_name!r} called with identical "
                f"args {self.max_repeats} times. Aborting to prevent runaway."
            )
        return await handler(args)
