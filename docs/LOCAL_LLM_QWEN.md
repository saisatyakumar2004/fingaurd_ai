# Real-model evaluation with local Qwen (Ollama) - 2026-10-18

**These are REAL-MODEL results** (`qwen2.5:3b`, Q4_K_M, 3.1B parameters, CPU inference on an i5-1335U / 8 GB RAM) on the **synthetic corruption suite** built from **live SEC data**
(40 companies, dataset snapshot `9255028dc22c0e5c`, suite `v1-s42-n20-e9a56f67`). They are not mock results and not production accuracy.
Provider: Ollama OpenAI-compatible endpoint `http://localhost:11434/v1` (`--provider openai`). Prompt version `v4`, temperature 0, seed 42, JSON mode on, max 700 output tokens.

## Runs
| Run | Config | Cases | Result |
|---|---|---|---|
| `run-20261018T165103Z-efbd8f` | prompt v2, `--limit 1` | 1 | **agent error `malformed_json`**: the model replied `{"action":"compare_values",...}` (tool name in `action`) without calling `lookup_company` first. Not counted as an answer. |
| `run-20261018T165345Z-45c6c5` | prompt v3 (worked example + tolerant parser), `--limit 1` | 1 | completed: 5 model calls, 4 tool calls, 99 s. **Failed**: flagged the right company and status but labelled `scale_mismatch` (copied the example's wording "exactly 10^3") instead of `period_mismatch`. |
| `run-20261018T171915Z-632afb` | prompt v3, `--mode smoke` | 24 | 7/24 passed, 4 agent errors (`invalid_agent_response`: `answer` field null when abstaining), 3 "verified" answers with **zero tool calls** (CONTRADICTION cases). 1,502 s. (Scored before the store-backed-evidence rule existed.) |
| **`run-20261018T174640Z-64ba9b`** | **prompt v4 (final)**, `--mode smoke` | **24** | **7/24 = 29.2%** - details below. 1,533 s. |

Prompt v4 changed two sentences after the v3 run (the `answer` field is mandatory even when abstaining; do not answer verified/flagged before calling tools). Those edits were made after seeing v3 failures on this same 24-case set, so v4 is *not* a held-out result; v3 and v4 are both reported.

## Final smoke result (prompt v4, 24 cases, all attempted and completed)
| Metric | Value |
|---|---|
| Strict accuracy (status + issue types + numbers + identity + evidence) | 7/24 = **0.292** |
| Status-only accuracy (lenient companion) | 15/24 = 0.625 |
| Numeric accuracy (cases with a numeric target) | 0.70 |
| Identity accuracy (CIK) | 1.00 |
| Detection precision / recall / F1 (error vs clean) | 1.00 / 0.65 / 0.79 |
| False-positive rate on clean controls | 0.0 (4/4 clean controls verified - only 4 samples) |
| Correct abstention rate / incorrect abstention rate / invented values | 1.00 (6 of 6 cases that required abstention) / 0.0 / 0 |
| High-confidence (>= 0.8) wrong answers | 11 |
| Evidence completeness (material claims cite store-returned refs) | 0.92 |
| Brier score of self-reported confidence | 0.43 (confidence is NOT a calibrated probability) |
| Agent errors / timeouts | 0 / 0 |
| Model calls / tool calls / cache | 104 / 80 / 0 hits, 24 misses |
| Tokens (prompt+generated) | 181,721 + 10,382 |
| Estimated cost | not available (local model; no prices configured) |
| Elapsed | 1,533 s (about 64 s/case; 15 s/call average) |

By category (strict): CLEAN_CONTROL 4/4 · SCALE_ERROR 3/4 · PERIOD_SHIFT 0/4 · MISSING_DATA 0/4 · ID_MISMAP 0/4 · CONTRADICTION 0/4.
Status-only: MISSING_DATA 4/4 and PERIOD_SHIFT 4/4 - the model abstained/flagged correctly but did not produce the required issue labels (`missing_input`, `period_mismatch`; it called every period error `scale_mismatch`).
ID_MISMAP 0/4: the model said `verified` although ticker and CIK resolve to different companies. CONTRADICTION 0/4: two cases `verified` with no tool use, two `insufficient_evidence` instead of conflict detection.
Abstention: the 6 cases that required abstention (4 MISSING_DATA, 2 conflicting-value CONTRADICTION) were all answered `insufficient_evidence` with no invented value; but 3 of those 6 still fail strictly because the issue labels were missing or wrong.

Reports: `reports/qwen_smoke_v4/llm_evaluation.{json,html}`, `reports/qwen_smoke_v4/scorecard.{json,html}`, stored run `reports/qwen_smoke_v4/runs/run-20261018T174640Z-64ba9b.json`, DB tables `agent_runs` / `evaluation_results`.

## What was changed to make real-model runs possible and honest
See the change list in the final report: `--limit`, flushed progress, health check, classified errors + bounded retries, one corrective re-ask, compact tool results, duplicate-call guard, tolerant dialect parser (counted), generation settings, cache key with settings fingerprint, interruption handling, evidence-must-be-store-backed scoring rule.

## Limitations
- Only the 24-case smoke set was run (the 120-case and 35-case multi-fault suites were NOT run on Qwen, by design: ~2 h each on this CPU).
- Single run per configuration, temperature 0, seed 42: no variance estimate. 4 clean controls cannot support a false-positive-rate claim.
- 3B-parameter model on CPU; larger models may behave very differently. Prompt v4 was tuned after looking at this same set.
- Synthetic faults on real SEC values; real-world error patterns are messier.
- Free RAM stayed near 0.75 GB during runs; nothing else was run concurrently.
