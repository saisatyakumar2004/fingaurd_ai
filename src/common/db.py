"""DuckDB access and schema.

Design notes
- Raw source payloads live on disk (data/raw) and are NEVER modified; this database only
  holds normalized/derived records plus pointers (source_snapshots) to the raw files.
- Primary keys are deterministic hashes of business keys, so re-ingesting identical data
  is idempotent and uniqueness is enforced where it matters.
- Monetary values are stored in the unit the source reported (e.g. 'USD'); conversion is
  done in a separate, documented layer (src/normalization/units.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import duckdb

SCHEMA: list[str] = [
    """CREATE TABLE IF NOT EXISTS source_snapshots (
        snapshot_id VARCHAR PRIMARY KEY,
        source_system VARCHAR NOT NULL,
        url VARCHAR NOT NULL,
        request_params VARCHAR,
        retrieval_timestamp TIMESTAMP NOT NULL,
        http_status INTEGER,
        sha256 VARCHAR NOT NULL,
        byte_size BIGINT,
        raw_path VARCHAR NOT NULL,
        status VARCHAR NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS companies (
        company_id VARCHAR PRIMARY KEY,
        cik BIGINT NOT NULL,
        ticker VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        sec_entity_name VARCHAR,
        sector VARCHAR,
        enabled_concepts VARCHAR,
        universe_verified BOOLEAN,
        universe_note VARCHAR,
        updated_at TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS company_identifiers (
        identifier_id VARCHAR PRIMARY KEY,   -- hash(company_id, id_type, id_value): uniqueness
        company_id VARCHAR NOT NULL,
        id_type VARCHAR NOT NULL,            -- CIK | TICKER | LEI | FIGI | COMPOSITE_FIGI | SHARE_CLASS_FIGI
        id_value VARCHAR NOT NULL,
        level VARCHAR NOT NULL,              -- entity | listing | instrument
        source_system VARCHAR,
        verification_status VARCHAR NOT NULL,
        evidence_snapshot_id VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS financial_observations (
        observation_id VARCHAR PRIMARY KEY,
        company_id VARCHAR NOT NULL,
        identifier VARCHAR NOT NULL,         -- zero-padded CIK as carried by the source
        concept VARCHAR NOT NULL,            -- revenue | net_income | total_assets
        xbrl_tag VARCHAR,
        tag_priority INTEGER,
        period_type VARCHAR NOT NULL,        -- annual | quarterly
        fiscal_year INTEGER,                 -- period-end calendar year (NOT company fiscal-year naming)
        fiscal_period VARCHAR,               -- FY2023 | 2023-CQ1
        reported_fy INTEGER,                 -- fy/fp as printed by SEC for the *filing* (provenance only)
        reported_fp VARCHAR,
        fiscal_period_start DATE,
        fiscal_period_end DATE NOT NULL,
        filing_date DATE,
        accession_number VARCHAR,
        form VARCHAR,
        amended BOOLEAN,
        value DOUBLE,
        unit VARCHAR,
        currency VARCHAR,
        source_system VARCHAR,
        source_url VARCHAR,
        retrieval_timestamp TIMESTAMP,
        snapshot_id VARCHAR,
        raw_ref VARCHAR,
        is_selected BOOLEAN DEFAULT FALSE
    )""",
    """CREATE TABLE IF NOT EXISTS sec_ticker_map (
        ticker VARCHAR NOT NULL,
        cik BIGINT NOT NULL,
        title VARCHAR,
        snapshot_id VARCHAR,
        PRIMARY KEY (ticker, cik)
    )""",
    """CREATE TABLE IF NOT EXISTS identity_resolutions (
        resolution_id VARCHAR PRIMARY KEY,
        company_id VARCHAR NOT NULL,
        query VARCHAR NOT NULL,
        search_mode VARCHAR,
        status VARCHAR NOT NULL,             -- VERIFIED | CANDIDATE_UNIQUE | AMBIGUOUS | UNMATCHED | SOURCE_UNAVAILABLE
        selected_lei VARCHAR,
        candidates VARCHAR,                  -- JSON
        rationale VARCHAR,
        snapshot_id VARCHAR,
        created_at TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS instrument_mappings (
        mapping_id VARCHAR PRIMARY KEY,
        company_id VARCHAR NOT NULL,
        query_json VARCHAR NOT NULL,
        status VARCHAR NOT NULL,             -- matched | unmatched | error
        figi VARCHAR,
        composite_figi VARCHAR,
        share_class_figi VARCHAR,
        ticker VARCHAR,
        name VARCHAR,
        exch_code VARCHAR,
        security_type VARCHAR,
        security_type2 VARCHAR,
        market_sector VARCHAR,
        snapshot_id VARCHAR,
        message VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS validation_runs (
        validation_run_id VARCHAR PRIMARY KEY,
        started_at TIMESTAMP,
        finished_at TIMESTAMP,
        blocked BOOLEAN,
        data_fingerprint VARCHAR,
        summary VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS quality_issues (
        issue_id VARCHAR NOT NULL,
        validation_run_id VARCHAR NOT NULL,
        rule_id VARCHAR NOT NULL,
        severity VARCHAR NOT NULL,           -- critical | high | medium | low
        classification VARCHAR NOT NULL,     -- confirmed_error | needs_review
        affected_record VARCHAR,
        expected_condition VARCHAR,
        observed_condition VARCHAR,
        evidence VARCHAR,                    -- JSON
        created_at TIMESTAMP,
        PRIMARY KEY (validation_run_id, issue_id)
    )""",
    """CREATE TABLE IF NOT EXISTS test_cases (
        case_id VARCHAR PRIMARY KEY,
        suite_version VARCHAR NOT NULL,
        corruption_type VARCHAR NOT NULL,
        subtype VARCHAR,
        random_seed BIGINT,
        affected_record_id VARCHAR,
        corruption_parameters VARCHAR,       -- JSON
        original_value VARCHAR,              -- JSON
        corrupted_value VARCHAR,             -- JSON
        expected_behavior VARCHAR,
        expected_answer VARCHAR,             -- JSON
        expected_abstention_or_escalation BOOLEAN,
        source_evidence VARCHAR,             -- JSON
        severity VARCHAR NOT NULL,
        smoke BOOLEAN,
        agent_input VARCHAR,                 -- JSON: the ONLY part an agent may see
        store_overrides VARCHAR,             -- JSON: hidden; shapes the case-scoped reference view
        expected VARCHAR,                    -- JSON: hidden scoring spec
        generated_at TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS agent_runs (
        run_id VARCHAR PRIMARY KEY,
        run_timestamp TIMESTAMP NOT NULL,
        git_commit VARCHAR,
        dataset_snapshot VARCHAR,
        model_id VARCHAR,
        agent_profile VARCHAR,
        prompt_version VARCHAR,
        evaluation_config VARCHAR,
        test_suite_version VARCHAR,
        mode VARCHAR,
        n_cases INTEGER,
        status VARCHAR,
        execution_json VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS evaluation_results (
        run_id VARCHAR NOT NULL,
        case_id VARCHAR NOT NULL,
        passed BOOLEAN,
        status VARCHAR,
        confidence DOUBLE,
        root_cause VARCHAR,
        severity VARCHAR,
        result_json VARCHAR,
        PRIMARY KEY (run_id, case_id)
    )""",
    """CREATE TABLE IF NOT EXISTS regression_baselines (
        baseline_name VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        created_at TIMESTAMP,
        metrics_json VARCHAR,
        case_results_json VARCHAR,
        run_meta_json VARCHAR
    )""",
]


def connect(path: str | Path = ":memory:", read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in SCHEMA:
        con.execute(stmt)


def open_db(path: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    con = connect(path)
    init_schema(con)
    return con


def fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, list(params) if params else [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_rows(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, Any]], ignore: bool = False) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    verb = "INSERT OR IGNORE INTO" if ignore else "INSERT INTO"
    sql = f"{verb} {table} ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})"
    con.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    return len(rows)


def upsert_row(con: duckdb.DuckDBPyConnection, table: str, key_cols: list[str], row: dict[str, Any]) -> None:
    """Delete-then-insert as two separate auto-committed statements (avoids same-transaction
    PK delete/insert conflicts in DuckDB)."""
    where = " AND ".join(f"{k} = ?" for k in key_cols)
    con.execute(f"DELETE FROM {table} WHERE {where}", [row[k] for k in key_cols])
    insert_rows(con, table, [row])


def insert_snapshot(con: duckdb.DuckDBPyConnection, snap: dict[str, Any]) -> None:
    row = {k: snap.get(k) for k in (
        "snapshot_id", "source_system", "url", "request_params", "retrieval_timestamp",
        "http_status", "sha256", "byte_size", "raw_path", "status")}
    exists = con.execute("SELECT 1 FROM source_snapshots WHERE snapshot_id = ?", [row["snapshot_id"]]).fetchone()
    if not exists:
        insert_rows(con, "source_snapshots", [row])
