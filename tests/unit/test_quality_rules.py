import math

from src.common.db import fetch_dicts
from src.quality.validate import gate_status, run_validation
from tests.helpers import build_db


def issues_for(con, rule):
    return fetch_dicts(con, "SELECT * FROM quality_issues WHERE rule_id = ? AND validation_run_id = "
                            "(SELECT validation_run_id FROM validation_runs ORDER BY validation_run_id DESC LIMIT 1)", [rule])


def test_clean_synthetic_db_has_no_blockers_and_gate_open():
    con, settings = build_db()
    s = run_validation(con, settings)
    assert s["blocked"] is False and s["blocking_issues"] == 0
    assert gate_status(con)["blocked"] is False
    # restated FY2021 revenue for the first company is surfaced as a *review* item, not an error
    dq9 = issues_for(con, "DQ009")
    assert dq9 and all(i["classification"] == "warning" for i in dq9)   # restatement = warning, not error
    assert len(dq9) == 3     # ONE aggregated finding per (company, concept), not one per period/filing


def _run(con, settings):
    run_validation(con, settings)


def test_gate_blocks_without_validation_and_after_data_change():
    con, settings = build_db()
    assert gate_status(con)["blocked"]
    run_validation(con, settings)
    assert not gate_status(con)["blocked"]
    con.execute("UPDATE financial_observations SET value = value * 2 WHERE observation_id = "
                "(SELECT min(observation_id) FROM financial_observations)")
    assert gate_status(con)["blocked"]       # stale validation


def test_dq006_wrong_cik_is_critical_and_blocks():
    con, settings = build_db()
    con.execute("DELETE FROM sec_ticker_map WHERE ticker = 'TSTA'")
    con.execute("INSERT INTO sec_ticker_map VALUES ('TSTA', 1, 'ALPHA TEST CORP', 'sec_edgar-tickers0000000')")
    s = run_validation(con, settings)
    assert s["blocked"] and any(i["severity"] == "critical" for i in issues_for(con, "DQ006"))
    assert "DQ006" in " ".join(gate_status(con)["reasons"])


def test_dq006_observation_identifier_mismatch():
    con, settings = build_db()
    con.execute("UPDATE financial_observations SET identifier = '0000000001' WHERE observation_id = "
                "(SELECT min(observation_id) FROM financial_observations)")
    run_validation(con, settings)
    assert any("identifier" in i["observed_condition"] for i in issues_for(con, "DQ006"))


def test_dq012_nan_and_negative_assets():
    con, settings = build_db()
    ids = [r[0] for r in con.execute("SELECT observation_id FROM financial_observations WHERE concept='total_assets' "
                                      "AND period_type='annual' LIMIT 2").fetchall()]
    con.execute("UPDATE financial_observations SET value = ? WHERE observation_id = ?", [float("nan"), ids[0]])
    con.execute("UPDATE financial_observations SET value = -5 WHERE observation_id = ?", [ids[1]])
    s = run_validation(con, settings)
    sev = {i["severity"] for i in issues_for(con, "DQ012")}
    assert {"critical", "high"} <= sev and s["blocked"]


def test_dq003_dq004_units():
    con, settings = build_db()
    ids = [r[0] for r in con.execute("SELECT observation_id FROM financial_observations LIMIT 2").fetchall()]
    con.execute("UPDATE financial_observations SET unit = 'USDthousands' WHERE observation_id = ?", [ids[0]])
    con.execute("UPDATE financial_observations SET unit = 'EUR' WHERE observation_id = ?", [ids[1]])
    run_validation(con, settings)
    assert [i["affected_record"] for i in issues_for(con, "DQ003")] == [ids[0]]
    assert [i["affected_record"] for i in issues_for(con, "DQ004")] == [ids[1]]


def test_dq005_invalid_period():
    con, settings = build_db()
    con.execute("UPDATE financial_observations SET fiscal_period_start = DATE '2030-01-01' WHERE observation_id = "
                "(SELECT min(observation_id) FROM financial_observations WHERE concept = 'revenue')")
    run_validation(con, settings)
    assert issues_for(con, "DQ005")


def test_dq002_duplicate_rows_detected():
    con, settings = build_db()
    con.execute("""INSERT INTO financial_observations
        SELECT 'dup-' || observation_id, company_id, identifier, concept, xbrl_tag, tag_priority, period_type, fiscal_year,
               fiscal_period, reported_fy, reported_fp, fiscal_period_start, fiscal_period_end, filing_date, accession_number,
               form, amended, value, unit, currency, source_system, source_url, retrieval_timestamp, snapshot_id, raw_ref, FALSE
        FROM financial_observations LIMIT 1""")
    run_validation(con, settings)
    dq2 = issues_for(con, "DQ002")
    assert dq2 and dq2[0]["classification"] == "confirmed_error"


def test_dq010_big_jump_and_dq011_missing_concept_and_dq013_provenance():
    con, settings = build_db()
    con.execute("UPDATE financial_observations SET value = value * 50 WHERE concept = 'revenue' AND is_selected "
                "AND period_type = 'annual' AND fiscal_period_end = DATE '2023-12-31' AND company_id = 'CIK0009000001'")
    con.execute("DELETE FROM financial_observations WHERE concept = 'net_income' AND company_id = 'CIK0009000002'")
    con.execute("UPDATE financial_observations SET accession_number = NULL WHERE observation_id = "
                "(SELECT min(observation_id) FROM financial_observations)")
    run_validation(con, settings)
    assert issues_for(con, "DQ010") and issues_for(con, "DQ011") and issues_for(con, "DQ013")


def test_dq007_dq008_identity_review_items():
    con, settings = build_db()
    con.execute("UPDATE identity_resolutions SET status = 'AMBIGUOUS', selected_lei = NULL WHERE company_id = 'CIK0009000001'")
    con.execute("UPDATE instrument_mappings SET status = 'unmatched' WHERE company_id = 'CIK0009000002'")
    run_validation(con, settings)
    assert issues_for(con, "DQ007") and issues_for(con, "DQ008")
    assert all(i["classification"] == "ambiguous" for i in issues_for(con, "DQ007"))
    assert all(i["classification"] == "needs_review" for i in issues_for(con, "DQ008"))


def test_dq001_missing_identifier():
    con, settings = build_db()
    con.execute("UPDATE financial_observations SET identifier = '' WHERE observation_id = "
                "(SELECT min(observation_id) FROM financial_observations)")
    s = run_validation(con, settings)
    assert issues_for(con, "DQ001") and s["blocked"]
    assert not math.isnan(1.0)


# ---------------------------------------------------------------- precedence / equivalence / conflict semantics
def _clone(con, src_id, new_id, **over):
    cols = [r[0] for r in con.execute("DESCRIBE financial_observations").fetchall()]
    row = dict(zip(cols, con.execute("SELECT * FROM financial_observations WHERE observation_id = ?", [src_id]).fetchone()))
    row.update(over, observation_id=new_id)
    con.execute(f"INSERT INTO financial_observations ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})", [row[c] for c in cols])


def _first_ni(con):
    return con.execute("SELECT observation_id FROM financial_observations WHERE concept='net_income' AND period_type='annual' "
                       "AND company_id='CIK0009000002' AND fiscal_period_end = DATE '2023-12-31' ORDER BY observation_id LIMIT 1").fetchone()[0]


def test_equivalent_tag_with_same_value_is_not_a_finding_but_is_counted():
    from src.quality.rules import equivalent_duplicate_stats
    con, settings = build_db()
    _clone(con, _first_ni(con), "alt-tag-same-value", xbrl_tag="ProfitLoss", tag_priority=1)
    run_validation(con, settings)
    assert not issues_for(con, "DQ002") and not issues_for(con, "DQ009")[3:]
    assert equivalent_duplicate_stats(con)["equivalent_multi_tag_groups_not_reported"] == 1


def test_different_measure_under_other_tag_is_resolved_by_precedence_not_reported_as_conflict():
    from src.quality.rules import equivalent_duplicate_stats
    from src.normalization.sec_normalize import reselect_all
    con, settings = build_db()
    base = _first_ni(con)
    _clone(con, base, "profitloss-later-filing", xbrl_tag="ProfitLoss", tag_priority=1, value=1.0, filing_date=__import__("datetime").date(2030, 1, 1))
    reselect_all(con)
    sel = con.execute("SELECT xbrl_tag FROM financial_observations WHERE company_id='CIK0009000002' AND concept='net_income' "
                      "AND fiscal_period_end = DATE '2023-12-31' AND period_type='annual' AND is_selected").fetchall()
    assert sel == [("NetIncomeLoss",)]            # precedence beats a later filing under a lower-priority tag
    run_validation(con, settings)
    assert len(issues_for(con, "DQ009")) == 3     # still only the pre-existing synthetic restatements
    assert equivalent_duplicate_stats(con)["different_measure_multi_tag_groups_resolved_by_precedence"] == 1


def test_same_tag_same_filing_different_value_is_a_confirmed_error():
    con, settings = build_db()
    _clone(con, _first_ni(con), "same-filing-conflict", value=123.0)
    run_validation(con, settings)
    dq9 = [i for i in issues_for(con, "DQ009") if i["classification"] == "confirmed_error"]
    assert len(dq9) == 1 and dq9[0]["severity"] == "high"


def test_exact_duplicate_same_tag_is_error_and_net_income_swing_is_not_flagged_by_dq010():
    con, settings = build_db()
    con.execute("UPDATE financial_observations SET value = value * 80 WHERE concept='net_income' AND is_selected "
                "AND period_type='annual' AND fiscal_period_end = DATE '2023-12-31' AND company_id='CIK0009000003'")
    run_validation(con, settings)
    assert not issues_for(con, "DQ010")
    con.execute("UPDATE financial_observations SET value = value * 80 WHERE concept='revenue' AND is_selected "
                "AND period_type='annual' AND fiscal_period_end = DATE '2023-12-31' AND company_id='CIK0009000003'")
    run_validation(con, settings)
    assert issues_for(con, "DQ010")


def test_issue_ids_are_stable_across_runs():
    con, settings = build_db()
    run_validation(con, settings)
    a = {r["issue_id"] for r in issues_for(con, "DQ009")}
    run_validation(con, settings)
    b = {r["issue_id"] for r in fetch_dicts(con, "SELECT issue_id FROM quality_issues WHERE rule_id='DQ009' AND validation_run_id = "
                                                 "(SELECT validation_run_id FROM validation_runs ORDER BY validation_run_id DESC LIMIT 1)")}
    assert a and a <= b
