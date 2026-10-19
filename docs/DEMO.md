# Demo walkthrough (about 5 minutes, after a successful first run)

1. `python -m src.quality.validate` — show the issue table: confirmed errors vs needs-review, and `GATE: OPEN`.
   (Demo of blocking: edit a CIK in `config/universe.yaml`, re-ingest with `--offline`, validate → `DQ006` critical, `BLOCKED`;
   `python -m src.evaluation.run` then exits 3 with the reason.)
2. `python -m src.corruption.generate --seed 42` — prints counts per category and two examples each: what the agent sees vs
   the expected behaviour. Point out that the agent input has no labels.
3. `python -m src.evaluation.run --mode full --agent mock-naive` — a gullible agent: near-zero recall, many high-confidence wrong answers.
4. `python -m src.evaluation.run --mode full --agent mock` then `python -m src.reporting.generate` — open `reports\scorecard.html`:
   per-category accuracy, detection precision/recall, clean-control false-positive rate, abstention, reliability diagram, severity-ranked failures
   (wrong company / wrong period first), and per-case evidence traces.
5. `python -m src.regression.compare --set-baseline`, then run `--agent mock-degraded` and `python -m src.regression.compare`:
   exit code 1; the output names newly failing cases and the breached rules (critical categories) even if overall accuracy looks acceptable.
6. Swap in a real model: `pip install anthropic`, set `ANTHROPIC_API_KEY`, run `--agent llm --mode smoke`, compare its scorecard to the baseline.

## 3-minute interview script
1. `python -m src.common.doctor` then `python -m pytest -o addopts=""` (85 pass, live tests shown as skipped, not passed).
2. `python -m src.quality.validate` - explain the XOM finding in `config/universe.yaml` (real data caught a stale CIK; the gate blocked evaluation).
3. `python -m src.corruption.generate --seed 42` - 120 cases, agent sees records only.
4. `python -m src.evaluation.run --mode full --agent mock` ; `python -m src.regression.compare --set-baseline`.
5. `python -m src.evaluation.run --mode full --agent mock-degraded` ; `python -m src.regression.compare` -> exit 1; open `reports/regression_diff.html`.
6. Be explicit: mock scores test the harness; a real model run is the next step.
