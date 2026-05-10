from __future__ import annotations

import instructor
from pydantic import BaseModel, Field

_JUDGE_SYSTEM = """\
You are a faithfulness evaluator for a knowledge-base compile pipeline.

You are given:
1. WAL (write-ahead log) entries — the raw input contributed by users and agents.
2. A compiled page body — the output written to the knowledge vault by the compile agent.

Your task: evaluate how faithfully the compiled page body represents the content in the
WAL entries. Score on a scale from 0.0 to 1.0:

  1.0 — Every factual claim, decision, version number, person, and date present in the
         WAL appears verbatim or accurately paraphrased in the compiled page. Nothing from
         the WAL is silently dropped. No content was invented.
  0.7 — Most claims are faithfully represented; minor paraphrasing is acceptable. One or
         two claims are slightly imprecise but not misleading.
  below 0.7 — Unsupported content is present (facts not in the WAL), or important WAL
               claims are missing, or the agent appears to have confabulated.

For each claim that failed (present in WAL but missing/wrong in page, or in page but not
in WAL), list it in failed_claims. Return an empty list if the page is fully faithful.
"""


class JudgeOutput(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str
    failed_claims: list[str] = Field(default_factory=list)


async def judge_faithfulness(
    wal_text: str,
    page_body: str,
    *,
    model: str = "claude-sonnet-4-6",
) -> JudgeOutput:
    """Evaluate WAL-to-page faithfulness using an LLM judge (instructor).

    Requires ANTHROPIC_API_KEY in the environment. Guarded by callers — only invoked
    when KEENYSPACE_EVAL_ANTHROPIC_API_KEY is set (nightly CI + live eval path).

    Returns a JudgeOutput with score in [0.0, 1.0], reasoning, and failed_claims.
    Callers should treat scores below 0.7 as a faithfulness regression.
    """
    async_client = instructor.from_provider(
        f"anthropic/{model}",
        async_client=True,
        mode=instructor.Mode.TOOLS,
    )
    return await async_client.create(
        response_model=JudgeOutput,
        max_retries=2,
        max_tokens=2_000,
        messages=[
            {
                "role": "user",
                "content": (
                    f"WAL entries:\n{wal_text}\n\n"
                    f"Compiled page body:\n{page_body}\n\n"
                    f"Evaluate faithfulness and score 0.0 to 1.0.\n\n"
                    f"System context:\n{_JUDGE_SYSTEM}"
                ),
            }
        ],
    )
