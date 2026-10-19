# FinGuard AI — Financial Data Quality and AI Agent Evaluation Platform

A reproducible framework that tests whether an AI agent can detect financial-data errors, resolve company
identity, abstain when evidence is insufficient, and explain itself with traceable evidence.
It is a **data-quality testing and evaluation harness**, not a chatbot.

> **Verification status (2026-10-17).** 127 offline tests pass (4 live tests skipped by default) on Python 3.12.10 and 3.11.17.
> Live SEC + GLEIF + OpenFIGI ingestion of 40 companies succeeded; 12 observations were checked against SEC filing text (11 verified, 1 weak context).
> **Real-LLM evaluation is NOT RUN** (no credentials); mock-agent scores validate the harness only. Not production-ready.
> Evidence: [docs/FINAL_GAP_AUDIT.md](docs/FINAL_GAP_AUDIT.md), [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md), [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Pipeline

```
SEC EDGAR / GLEIF / OpenFIGI --(cache raw, hash, never overwrite)--> data/raw
        --> normalize --> DuckDB reference store --> deterministic quality rules --> GATE
        --> seeded corruption engine --> test cases (agent-visible input | hidden expectations)
        --> agent (tools over case-scoped reference store) --> scoring --> metrics
        --> scorecard.json / scorecard.html --> regression vs baseline
```
Architecture diagram: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements
Windows 11 (any OS works), Python 3.11+, ~8 GB RAM. No Docker, no database server, no paid API for the baseline.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env      # then edit .env (gitignored): SEC_USER_AGENT=FinGuardAI <your-dedicated-project-email>
python -m src.common.doctor # dependency / configuration check
```

## First run (do these in order)

1. **Offline tests – no internet, no credentials**
   ```powershell
   python -m pytest -o addopts=""      # add -m live --run-live for the opt-in live suite
   ```
   Uses a *synthetic* SEC-shaped fixture (fake companies/CIKs/FIGIs). If anything fails, fix that first.

2. **Collect real data (needs internet + `SEC_USER_AGENT`)** — only this step and later `--refresh` need the network.
   ```powershell
   python -m src.ingestion.run            # SEC + GLEIF + OpenFIGI, cached under data\raw
   python scripts\inspect_companyfacts.py # see which XBRL tags each company really uses
   ```
   Re-run any time without internet using the cached snapshots: `python -m src.ingestion.run --offline`.
   OpenFIGI without an API key is slow by design (rate limit). Add `OPENFIGI_API_KEY` to speed it up.

3. **Validate → generate → evaluate → report**
   ```powershell
   python -m src.quality.validate                 # exit 1 and BLOCKED if critical confirmed errors exist
   python -m src.corruption.generate --seed 42    # 6 categories x 20 = 120 cases
   python -m src.evaluation.run --mode smoke --agent mock
   python -m src.evaluation.run --mode full  --agent mock
   python -m src.reporting.generate               # reports\scorecard.json / scorecard.html
   ```
   Or all at once: `.\scripts\run_pipeline.ps1` (`-Offline` to reuse cache).

4. **Regression**
   ```powershell
   python -m src.regression.compare --set-baseline          # save current run as baseline
   python -m src.evaluation.run --mode full --agent mock-degraded
   python -m src.regression.compare                          # exits 1: critical categories regressed (exit 4 if baseline is incompatible: re-baseline explicitly)
   ```

If validation reports `DQ006` critical errors, a CIK in `config/universe.yaml` does not match SEC's ticker file —
fix the universe entry (that is exactly what the check is for). Other items (`DQ011` missing concept, e.g. a bank
without a `Revenues` tag) are *needs_review* warnings and do not block.

## Agents
| `--agent` | What it is |
|---|---|
| `mock` | deterministic rule-based reference agent (validates the harness, not any LLM) |
| `mock-naive` | trusts everything — negative control; must score badly |
| `mock-degraded` | blind to scale/period errors — used to demonstrate regression gating |
| `llm` | optional real model via `AnthropicProvider` (`pip install anthropic`, `ANTHROPIC_API_KEY`, `FINGUARD_LLM_MODEL`) or `--provider openai` (OpenAI-compatible); responses cached by case+model+prompt+snapshot |

## Test categories (≥20 each, default 120 cases)
`SCALE_ERROR`, `ID_MISMAP`, `MISSING_DATA`, `CONTRADICTION`, `PERIOD_SHIFT` (value attributed to the wrong fiscal period), `CLEAN_CONTROL`.
Cases get opaque ids, are sorted by id, and the agent only ever receives `{case_id, task, records}`; expected answers,
labels and store overrides stay in the evaluator ([tests/integration/test_pipeline.py](tests/integration/test_pipeline.py) asserts no leakage).

## Reproducing a run
Raw responses are immutable files with SHA-256 in `data/raw/<source>/<name>/`; `source_snapshots` rows point to them and
`agent_runs` records git commit, dataset-snapshot hash, model, prompt version, evaluation config and suite version.
Same cache + same seed + same agent ⇒ same case ids and same scores. LLM runs reuse `data/cache/llm`.

## Layout
`config/` universe + settings · `src/{ingestion,normalization,identity,quality,corruption,agent,evaluation,reporting,regression}` ·
`tests/{unit,integration}` · `scripts/` · `dashboard/app.py` (optional Streamlit: `streamlit run dashboard/app.py`) · `docs/`.

See also: [DATA_SOURCES.md](DATA_SOURCES.md), [docs/DEMO.md](docs/DEMO.md), [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Command reference (every command below was executed during the audit)
| Purpose | Command |
|---|---|
| Environment check | `python -m src.common.doctor` |
| Unit tests | `python -m pytest -o addopts="" -m unit` |
| Offline integration tests | `python -m pytest -o addopts="" -m integration` |
| End-to-end mock evaluation tests | `python -m pytest -o addopts="" -m e2e` |
| All offline tests | `python -m pytest -o addopts=""` |
| Live API tests (internet, SEC_USER_AGENT) | `python -m pytest -o addopts="" -m live --run-live` |
| Ingest (live / cached) | `python -m src.ingestion.run [--tickers AAPL,MSFT] [--offline]` |
| Snapshot integrity | `python -m src.ingestion.verify` |
| Validate reference data (gate) | `python -m src.quality.validate` |
| Independent filing check | `python scripts/verify_against_filings.py` |
| Generate base suite (120) / multi-fault (35) | `python -m src.corruption.generate --seed 42` / `... --multi` |
| Mock evaluation | `python -m src.evaluation.run --mode {smoke,full} --suite {base,multi,all} --agent {mock,mock-naive,mock-degraded}` |
| Real LLM evaluation (blinded 30, then full) | `python -m src.evaluation.run --agent llm --mode smoke30` / `--mode full` (exit 5 + NOT_RUN report without credentials) |
| Reports | `python -m src.reporting.generate` -> `reports/scorecard.{json,html}` |
| Regression | `python -m src.regression.compare --set-baseline` then `python -m src.regression.compare` (exit 1 breach, 4 incompatible baseline) |
| Synthetic demo DB | `python scripts/build_synthetic_db.py data/synthetic.duckdb` (fake data; use `--db`) |
| Dashboard (optional) | `pip install streamlit` then `streamlit run dashboard/app.py` |

Python 3.11 without a `py -3.11` entry: `python -m pip install uv; python -m uv venv .venv --python 3.11` (this is how 3.11.17 was obtained here).


## Local model (Ollama + Qwen) - how to run the real-LLM path
Prerequisites: Ollama running, `ollama pull qwen2.5:3b`. In `.env` (never committed):
```
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
FINGUARD_LLM_MODEL=qwen2.5:3b
```
```powershell
# 1 case (deterministic: the first case of the selected suite/mode in case-id order). Health check runs first.
python -m src.evaluation.run --agent llm --provider openai --mode smoke --limit 1
# all 24 smoke cases (~75 s per case on an i5 CPU => ~30 min). Ctrl+C is safe: completed cases are saved (exit 130).
python -m src.evaluation.run --agent llm --provider openai --mode smoke
# native Ollama API (lets you set num_ctx / num_predict / keep_alive):
python -m src.evaluation.run --agent llm --provider ollama --mode smoke --limit 1
```
Each case prints `[i/N] case-id CATEGORY`, one line per model call (seconds, output tokens) and a final PASS/FAIL line with status, elapsed time, model/tool calls and cache hit/miss.
Exit codes: 0 ok, 2 error, 3 data-quality gate blocked, 5 NOT RUN (no credentials/package), 6 provider/model health check failed, 130 interrupted.
Generation settings: `config/settings.yaml: llm:` or `FINGUARD_MAX_OUTPUT_TOKENS`, `FINGUARD_NUM_CTX`, `FINGUARD_TEMPERATURE`, `FINGUARD_READ_TIMEOUT`, `FINGUARD_JSON_MODE`, `FINGUARD_MAX_ATTEMPTS`, `FINGUARD_TIMEOUT_RETRIES`.
Run only ONE model evaluation at a time on an 8 GB machine. Results and caveats: [docs/LOCAL_LLM_QWEN.md](docs/LOCAL_LLM_QWEN.md).
