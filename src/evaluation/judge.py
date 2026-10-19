"""Optional LLM-as-judge scaffolding (NOT RUN by default).

The judge is never ground truth: deterministic scoring decides pass/fail. A judge may only add an
explanation-quality opinion, and its labels must be compared with a human-labelled sample first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.evaluation.metrics import cohens_kappa

RUBRIC_VERSION = "rubric-v1"
RUBRIC = {
    "evidence_relevance": "Do cited refs support the claim? (0 none, 1 partly, 2 fully)",
    "factual_consistency": "Does the explanation agree with the cited evidence and the final status? (0/1/2)",
    "reasoning_completeness": "Does it state basis, issue type and next action? (0/1/2)",
}


@dataclass
class JudgeRecord:
    judge_model: str
    rubric_version: str
    prompt_version: str


def agreement_report(case_ids: list[str], judge: list[str], human: list[str]) -> dict[str, Any]:
    if not (len(case_ids) == len(judge) == len(human)) or not case_ids:
        raise ValueError("judge/human labels must be non-empty and aligned with case_ids")
    disagree = [{"case_id": c, "judge": j, "human": h} for c, j, h in zip(case_ids, judge, human) if j != h]
    return {"n": len(case_ids), "percent_agreement": 1 - len(disagree) / len(case_ids),
            "cohens_kappa": cohens_kappa(judge, human), "disagreements": disagree,
            "rubric_version": RUBRIC_VERSION,
            "note": "The judge must not be used as the sole source of ground truth."}
