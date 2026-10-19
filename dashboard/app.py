"""Optional local dashboard.  pip install streamlit ; streamlit run dashboard/app.py"""
import json
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "reports" / "scorecard.json"
st.set_page_config(page_title="FinGuard AI", layout="wide")
st.title("FinGuard AI scorecard")
if not path.exists():
    st.warning("reports/scorecard.json not found. Run the pipeline first.")
    st.stop()
sc = json.loads(path.read_text(encoding="utf-8"))
m = sc["metrics"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Cases", m["total"])
c2.metric("Accuracy", f"{m['accuracy']:.1%}")
c3.metric("Clean-control FPR", "n/a" if m["false_positive_rate"] is None else f"{m['false_positive_rate']:.1%}")
c4.metric("High-confidence wrong", m["high_confidence_wrong"]["count"])
st.subheader("Accuracy by category")
st.bar_chart({k: v["accuracy"] for k, v in m["by_category"].items()})
st.subheader("Root causes")
st.table(m["root_causes"] or [{"cause": "none", "count": 0, "weighted_score": 0}])
st.subheader("Cases")
fails_only = st.checkbox("Failures only", value=True)
rows = [c for c in sc["cases"] if not (fails_only and c["passed"])]
st.dataframe([{"case": c["case_id"], "category": c["category"], "severity": c["severity"], "passed": c["passed"],
               "status": c["agent"]["status"], "root_cause": c["root_cause"]} for c in rows])
for c in rows[:50]:
    with st.expander(f"{c['case_id']} ({c['category']})"):
        st.json(c)
for d in sc["disclaimers"]:
    st.caption(d)
