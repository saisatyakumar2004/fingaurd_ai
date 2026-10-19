# Architecture

```mermaid
flowchart LR
  subgraph Sources
    SEC[SEC EDGAR<br/>tickers + companyfacts]
    GL[GLEIF<br/>lei-records]
    FG[OpenFIGI<br/>v3 mapping]
  end
  SEC & GL & FG --> HTTP[HttpFetcher<br/>rate limit / retry / immutable snapshots]
  HTTP --> RAW[(data/raw<br/>JSON + sha256)]
  HTTP --> NORM[normalization + identity resolver]
  NORM --> DB[(DuckDB reference store)]
  DB --> DQ[Deterministic quality rules DQ001-DQ013]
  DQ -->|critical confirmed error| BLOCK{{Gate: block evaluation}}
  DQ -->|ok| GATE{{Gate open}}
  GATE --> CORR[Seeded corruption engine]
  CORR --> CASES[(test_cases)]
  CASES -->|agent_input only| AG[Agent under test]
  DB -->|case-scoped view| TOOLS[ReferenceStore tools]
  AG <--> TOOLS
  AG --> SCORE[Scoring + metrics]
  CASES -->|hidden expectations| SCORE
  SCORE --> RUNS[(agent_runs / evaluation_results)]
  RUNS --> REP[scorecard.json / .html]
  RUNS --> REG[Regression compare vs baseline]
```

## Responsibility split
| Package | Responsibility |
|---|---|
| `common` | config, DuckDB schema, immutable-snapshot HTTP fetcher, logging, ids |
| `ingestion` | one adapter per source (+ loaders, CLI) |
| `normalization` | SEC → observations; unit-conversion layer (storage never converts units) |
| `identity` | name matching as candidate generation, with explicit ambiguity |
| `quality` | 13 deterministic rules, confirmed-error vs needs-review, evaluation gate |
| `corruption` | reversible transforms, seeded case generator (no changes to golden data) |
| `agent` | schemas, case-scoped tools, mock agents, optional LLM adapter + cache |
| `evaluation` | pure scoring/metrics, runner (records run metadata) |
| `reporting` / `regression` | scorecards; baseline diff with per-category thresholds |

## Leakage control
`test_cases.agent_input` (records only) is the sole thing passed to an agent. `expected` and `store_overrides`
(which hide periods from the case-scoped store) are read only by the evaluator. Case ids are hashed and sorted so
category cannot be inferred from id or position.
