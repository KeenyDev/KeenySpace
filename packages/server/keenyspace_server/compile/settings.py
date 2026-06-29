from __future__ import annotations

from pydantic import BaseModel


class CompileSettings(BaseModel):
    """Compile-pipeline defaults. Loaded via Settings nesting (KEENYSPACE_COMPILE__*)."""

    debounce_seconds: int = 30
    backstop_interval_minutes: int = 15
    max_tool_calls: int = 20
    max_input_tokens: int = 50_000
    # Cap on a single LLM generation (ModelSettings.max_tokens). MUST stay <= the
    # chosen model's max output tokens (e.g. gpt-5.4-mini = 128_000).
    max_output_tokens_per_call: int = 20_000
    # Daily OUTPUT-token budget per workspace ("space"). Compile pauses with reason
    # 'space_budget_exceeded' once a workspace's summed compile output for the current
    # UTC day crosses this; the 00:00 UTC reset (reset_daily_ceiling) clears the tally
    # and resumes, and manual resume() clears it too. NO per-compile-run output throttle.
    max_output_tokens_per_space: int = 128_000
    max_seconds: int = 180
    daily_token_ceiling: int = 500_000
    # Provider is pydantic-ai's provider id (anthropic | openai | google-gla | ...).
    # `model` is the bare model name; the agent joins them as "<provider>:<model>".
    # A fully-qualified `model` ("openai:gpt-4o") overrides `provider`. Anthropic
    # stays the default (D-04); other providers are opt-in via KEENYSPACE_COMPILE__*.
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
