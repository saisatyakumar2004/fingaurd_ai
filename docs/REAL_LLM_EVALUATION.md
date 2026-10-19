# Real LLM evaluation

## Status: **NOT RUN**
No `ANTHROPIC_API_KEY` (or OpenAI-compatible key) exists on this machine and the optional `anthropic` package is not installed in the project venv.
`python -m src.evaluation.run --agent llm --mode smoke30` therefore exits **5**, prints `REAL-LLM EVALUATION: NOT RUN (...)` and writes
`reports/llm_evaluation.json` / `.html` with `status: NOT_RUN`, `metrics: null` (shown as "not available", never as zero). **Every live-model claim is unverified.**

## What is implemented and verified with scripted fake providers (plumbing only, not model quality)
- Provider interface (`complete(system, messages)`); `AnthropicProvider` (SDK, `ANTHROPIC_API_KEY`, model from `FINGUARD_LLM_MODEL`, default `claude-sonnet-5-5`) and an OpenAI-compatible provider. The model is swappable; scoring does not depend on it.
- Tool loop over the case-scoped reference store; Pydantic validation of the final answer (bad status/confidence -> error, no fabricated answer).
- Bounded retries (3 attempts, backoff); outage -> `ProviderUnavailable` recorded as an `agent_error` failure with `response = null`.
- Disk cache keyed by `(case_id, model, prompt_version, dataset snapshot, input hash)`; a different model id does not reuse an entry (tested).
- Token usage (provider-reported) and cost estimate (only if `FINGUARD_PRICE_IN_PER_MTOK` / `FINGUARD_PRICE_OUT_PER_MTOK` are set; otherwise `not available`).
- Run metadata: run id, model id, prompt version, dataset snapshot, suite version, git commit if available.
- The agent only receives `{case_id, task, records}`; leakage tests cover the tools (see TEST_COVERAGE.md).
- `smoke30` = first 5 cases per base category in opaque case-id order (30 cases, balanced, deterministic); `full` = 120; `--suite multi` = 35 extra cases.
- Defect found while testing this path: the retry/usage wrapper was wired to the wrong call site (dead code, and a self-recursion once swapped). Fixed; covered by `test_provider_outage_is_bounded...` and `test_tool_loop_usage...`.

## Metrics reported (from stored case outputs): numeric accuracy (unit-aware), identity accuracy, detection precision/recall/F1, clean-control FPR, correct and incorrect abstention rate, high-confidence-incorrect count, evidence completeness, tokens, cost, by-category accuracy. Self-reported confidence is not treated as a calibrated probability.

## To run it
```powershell
pip install anthropic
$env:ANTHROPIC_API_KEY = "<your key>"          # or put it in .env (gitignored)
python -m src.evaluation.run --agent llm --mode smoke30     # 30 cases
python -m src.evaluation.run --agent llm --mode full        # 120 cases
python -m src.evaluation.run --agent llm --mode full --suite multi
```
Do not publish numbers from a run whose `reports/llm_evaluation.json` status is not `COMPLETED`.
