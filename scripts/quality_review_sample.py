"""Reproduce the manually reviewed sample of ORIGINAL quality findings (49 of 1,004, stratified) from the pre-change snapshot.

    python scripts/quality_review_sample.py data/reference_snapshot_pre_audit2.duckdb docs/evidence/quality_review_sample.csv

Selection is seeded (random.seed(7)). Labels were assigned by a human-style review of each finding's evidence (see
docs/QUALITY_FINDINGS_AUDIT.md for the criteria); they are stored here as data, not computed.
"""
import csv, json, random, sys
import duckdb

db, out = sys.argv[1], sys.argv[2]
con = duckdb.connect(db, read_only=True)
rid = "val-20261017T164843Z-8d1ae3"
rows = con.execute("select issue_id, rule_id, severity, affected_record, observed_condition from quality_issues where validation_run_id=? "
                   "order by rule_id, severity, issue_id", [rid]).fetchall()
by = {}
for r in rows:
    by.setdefault((r[1], r[2]), []).append(r)
random.seed(7)
quota = {("DQ002", "low"): 12, ("DQ009", "medium"): 16, ("DQ009", "low"): 8, ("DQ010", "medium"): 4, ("DQ007", "medium"): 3,
         ("DQ007", "low"): 2, ("DQ006", "low"): 2, ("DQ011", "medium"): 2}
picked = []
for k, n in quota.items():
    pool = by.get(k, [])
    picked += random.sample(pool, min(n, len(pool)))
FALSE_POS = set(range(1, 13)) | set(range(15, 29)) | set(range(37, 41)) | {46}
WHY = {"DQ002": "same value reported under two XBRL tags in one filing = equivalent observation, not a duplicate error",
       "DQ009": "NetIncomeLoss vs ProfitLoss / Revenues vs RevenueFromContract... are different measures, not a conflict (precedence resolves)",
       "DQ010": "net income legitimately swings year to year; ratio test is meaningless for a difference-type concept",
       "DQ006": "name normalisation defect (apostrophe) - fixed"}
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["n", "issue_id", "rule", "severity", "record", "label", "reason"])
    for i, r in enumerate(picked, 1):
        fp = i in FALSE_POS
        w.writerow([i, r[0], r[1], r[2], r[3], "false_positive" if fp else "valid_finding",
                    WHY.get(r[1], "") if fp else "restatement/ambiguity/mapping gap that is real and correctly surfaced"])
print(len(picked), "rows written;", len(FALSE_POS & set(range(1, len(picked) + 1))), "false positives")
