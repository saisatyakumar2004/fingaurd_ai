"""LLM-as-judge call/parse layer. Live use is NOT RUN unless a provider is configured; tests use scripted fixtures.

The judge never receives expected answers or corruption metadata and is never ground truth.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.evaluation.judge import RUBRIC, RUBRIC_VERSION

JUDGE_PROMPT_VERSION = "judge-prompt-v1"
JUDGE_SYSTEM = (
    "You grade a financial-data agent's explanation. You see the agent's final JSON and the evidence the agent's tools returned. "
    "You do NOT know the correct answer and must not guess financial facts. Score each rubric item 0, 1 or 2 and reply with ONE JSON "
    'object: {"evidence_relevance":0-2,"factual_consistency":0-2,"reasoning_completeness":0-2,"comment":"..."}.')


@dataclass
class JudgeResult:
    case_id: str
    ok: bool
    scores: dict[str, int] | None
    raw: str                      # preserved verbatim, even when malformed
    error: str | None
    judge_model: str
    rubric_version: str = RUBRIC_VERSION
    prompt_version: str = JUDGE_PROMPT_VERSION


def parse_judge_output(case_id: str, raw: str, judge_model: str) -> JudgeResult:
    try:
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start:end + 1])
        scores = {k: int(obj[k]) for k in RUBRIC}
        if any(v not in (0, 1, 2) for v in scores.values()):
            raise ValueError("score outside 0..2")
        return JudgeResult(case_id, True, scores, raw, None, judge_model)
    except Exception as e:     # malformed output is recorded, never repaired or guessed
        return JudgeResult(case_id, False, None, raw, f"{type(e).__name__}: {e}", judge_model)


def judge_case(provider: Any, case_id: str, response: dict[str, Any], tool_evidence: list[dict[str, Any]]) -> JudgeResult:
    """`provider.complete(system, messages) -> str`."""
    payload = json.dumps({"agent_response": response, "tool_evidence": tool_evidence}, default=str)
    model = getattr(provider, "model_id", "unknown")
    try:
        raw = provider.complete(JUDGE_SYSTEM, [{"role": "user", "content": payload}])
    except Exception as e:
        return JudgeResult(case_id, False, None, "", f"provider_error: {type(e).__name__}", model)
    return parse_judge_output(case_id, raw, model)
