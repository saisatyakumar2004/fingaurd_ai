# Regression validation (real reference data, 2026-10-17)

Baseline integrity: a baseline stores run id, model id, prompt version, evaluation config, test-suite version, dataset snapshot, git commit (`null`: the folder is not a git repository), metrics and per-case results.
`regression.compare` **refuses** (exit 4) when suite version, dataset snapshot, evaluation configuration or the case set differ; model/prompt/commit differences are listed under "context changes", not hidden. Re-baselining is only via explicit `--set-baseline`.

Transcript: `reports/evidence/regression_demo_transcript.txt`. Diffs: `reports/evidence/regression_diff_FAIL_degraded_agent.{json,html}`, `regression_diff_PASS_restored_agent.json`.

| Step | Command | Result |
|---|---|---|
| S0 | compare against the baseline from the earlier dataset/suite | **exit 4** (suite version differs) - not silently compared |
| S1 | `--set-baseline` with reference agent (120 cases) | baseline `run-20261017T181110Z-11ce32`, accuracy 1.000 |
| S2 | `--agent mock-degraded` (blind to scale and period errors) | accuracy 0.667, recall 0.6, 40 high-confidence wrong |
| S3 | `regression.compare` | **FAIL, exit 1**: accuracy -0.333; SCALE_ERROR and PERIOD_SHIFT -1.0; recall -0.4; +40 high-confidence wrong; 20 newly failing critical cases; 40 changed answers |
| S4 | restore reference agent, rerun | accuracy 1.000 |
| S5 | `regression.compare` | **PASS, exit 0** (0 newly failing, 0 changed answers) |
| S6 | smoke run (24) vs full baseline (120) | **exit 4** (case sets differ) |
The baseline was never edited to hide the failure. Thresholds (`config/settings.yaml: regression`): overall accuracy drop 0.02, per-category drop 0.05, recall drop 0.05, FPR +0.02, high-confidence-wrong +0, critical categories (ID_MISMAP, PERIOD_SHIFT) 0 new failures. An overall improvement cannot hide a critical-category regression (unit test).
Limitation: the "defect" is a purpose-built degraded mock; regression detection on real LLM drift is untested (NOT RUN).
