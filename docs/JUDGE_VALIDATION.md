# LLM-as-judge validation

## Decision: judge **not enabled** - interface and tests only. Live judge: **NOT RUN**.
Justification test: a judge is only worth adding where deterministic scoring cannot assess the explanation. The deterministic rubric already
scores evidence relevance (cited refs must be refs the tools actually returned), factual consistency (status vs issues vs cited values) and
completeness. On the mock agent every explanation is a template, so a judge would add no signal; the benefit can only be measured on real model explanations, and none exist (REAL_LLM_EVALUATION.md: NOT RUN).

## Implemented (`src/evaluation/judge.py`, `judge_runner.py`)
- Versioned rubric (`rubric-v1`) and prompt (`judge-prompt-v1`); judge model id recorded per result.
- The judge sees only the agent's final JSON and the tool evidence - never expected answers or corruption metadata (tested with a spy provider).
- Raw output is preserved verbatim; malformed output, out-of-range scores and provider failures return `ok=False` with the error - never repaired or guessed (tested).
- `agreement_report` (percent agreement, Cohen's kappa, list of disagreements) for judge-vs-human comparison (arithmetic tested).
- Judge labels are never merged into the independently verified ground truth.

## Not done (blocked)
The required manually labelled sample of >= 30 real explanations does not exist: producing it from templated mock explanations would be meaningless, and fabricating labels is not acceptable. Acceptance requires: run a real model (smoke30+), hand-label >= 30 explanations, run the judge, report agreement.
