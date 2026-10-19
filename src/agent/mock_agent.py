"""Deterministic rule-based agents for development and offline tests.

IMPORTANT: results obtained with these agents validate the evaluation HARNESS, not the quality
of any language model.

profiles
  reference : checks identity, compares to the reference store, recognises scale / period errors
  naive     : trusts every record ("verified", high confidence)  -> negative control for the harness
  degraded  : like reference but blind to scale and period errors -> used to demo regression gating
"""
from __future__ import annotations

from typing import Any

from src.agent.schemas import AgentResponse, EvidenceItem
from src.agent.tools import ReferenceStore
from src.common.util import cik_int, pad_cik
from src.identity.resolver import name_similarity
from src.normalization.units import UnsupportedUnit, to_base, within_tolerance

PROFILES = ("reference", "naive", "degraded")
TOL = 0.005


class MockAgent:
    prompt_version = "mock-v1"

    def __init__(self, profile: str = "reference") -> None:
        if profile not in PROFILES:
            raise ValueError(f"unknown mock profile {profile!r}")
        self.profile = profile
        self.model_id = f"mock-{profile}"
        self.check_scale = profile != "degraded"
        self.check_period = profile != "degraded"

    # ------------------------------------------------------------------
    def run(self, agent_input: dict[str, Any], store: ReferenceStore) -> AgentResponse:
        case_id = agent_input["case_id"]
        records = agent_input["records"]
        if self.profile == "naive":
            return self._naive(case_id, records[0])
        if len(records) > 1:
            return self._multi(case_id, records, store)
        return self._single(case_id, records[0], store)

    # ------------------------------------------------------------------
    @staticmethod
    def _resp(case_id, status, answer, conf, explanation, action, issues=(), value=None, company=None,
              period=None, evidence=()) -> AgentResponse:
        return AgentResponse(
            case_id=case_id, status=status, answer=answer, normalized_value=value,
            unit="USD" if value is not None else None, company_identifier=company, fiscal_period=period,
            confidence=conf, evidence=list(evidence), explanation=explanation,
            detected_issue_types=list(issues), recommended_action=action)

    def _naive(self, case_id, rec) -> AgentResponse:
        val = None
        try:
            if rec.get("value") is not None:
                val, _ = to_base(float(rec["value"]), rec.get("unit"))
        except UnsupportedUnit:
            val = None
        return self._resp(
            case_id, "verified", "Record accepted as provided.", 0.95,
            "The record looks plausible and internally consistent, so it is accepted without further checks.",
            "No action required.", value=val, company=rec.get("cik"), period=rec.get("fiscal_period_end"),
            evidence=[EvidenceItem(source="input_record", ref=rec["record_id"], value=rec.get("value"), detail="record as provided")])

    # ---- identity -----------------------------------------------------
    def _identity(self, rec, store) -> tuple[dict | None, list[str], list[EvidenceItem]]:
        ev: list[EvidenceItem] = []
        by_t = by_c = None
        if rec.get("ticker"):
            r = store.call("lookup_company", {"identifier": rec["ticker"]})
            by_t = r["company"] if r.get("found") and not r.get("ambiguous") else None
        if rec.get("cik"):
            r = store.call("lookup_company", {"identifier": rec["cik"]})
            by_c = r["company"] if r.get("found") and not r.get("ambiguous") else None
        if by_t is None and rec.get("company_name"):
            r = store.call("lookup_company", {"identifier": rec["company_name"]})
            by_t = r["company"] if r.get("found") and not r.get("ambiguous") else None
        for c, how in ((by_t, "ticker/name"), (by_c, "cik")):
            if c:
                ev.append(EvidenceItem(source="reference_store", ref=c["company_id"], detail=f"{how} resolves to {c['name']} (CIK {c['cik']})"))
        problems: list[str] = []
        if rec.get("cik") and by_c is None:
            problems.append("record CIK is not present in the reference store")
        if by_t and by_c and cik_int(by_t["cik"]) != cik_int(by_c["cik"]):
            problems.append(f"ticker {rec.get('ticker')} resolves to CIK {by_t['cik']} but the record carries CIK {by_c['cik']}")
        if by_t is None and by_c and rec.get("company_name") and name_similarity(rec["company_name"], by_c["name"]) < 0.5:
            problems.append("record company name does not match the company that owns the record CIK")
        owner = by_t or by_c
        if rec.get("figi"):
            inst = store.call("lookup_instrument", {"figi": rec["figi"]})
            if inst.get("found") and owner and cik_int(inst["matches"][0]["company_cik"]) != cik_int(owner["cik"]):
                problems.append(f"FIGI {rec['figi']} belongs to CIK {inst['matches'][0]['company_cik']}, not {owner['cik']}")
                ev.append(EvidenceItem(source="reference_store", ref=rec["figi"], detail="FIGI maps to a different company"))
        return by_t or (by_c if not problems else None), problems, ev

    # ---- single record --------------------------------------------------
    def _single(self, case_id, rec, store) -> AgentResponse:
        ev = [EvidenceItem(source="input_record", ref=rec["record_id"], value=rec.get("value"), detail="record under review")]
        company, problems, id_ev = self._identity(rec, store)
        ev += id_ev
        if problems:
            ident = pad_cik(company["cik"]) if company else None
            status = "flagged" if company else "ambiguous"
            return self._resp(case_id, status, "Identifier inconsistency detected.", 0.9 if company else 0.55,
                              "; ".join(problems) + ".", "Hold the record; correct the identifier mapping and re-verify.",
                              issues=["identifier_collision"], company=ident, period=rec.get("fiscal_period_end"), evidence=ev)
        if company is None:
            return self._resp(case_id, "insufficient_evidence", "Company could not be resolved.", 0.5,
                              "Neither ticker, CIK nor name resolves to a company in the reference store.",
                              "Escalate for manual entity resolution.", issues=["missing_input"], evidence=ev)
        cik10 = pad_cik(company["cik"])
        concept, pend, ptype = rec.get("concept"), rec.get("fiscal_period_end"), rec.get("period_type", "annual")
        if not concept or not pend:
            return self._resp(case_id, "insufficient_evidence", "Required period metadata is missing.", 0.85,
                              "The record has no fiscal period (or concept), so the value cannot be attributed to a reporting period.",
                              "Request the missing period metadata before using this value.", issues=["missing_input"],
                              company=cik10, evidence=ev)
        res = store.call("get_observations", {"company": cik10, "concept": concept, "period_end": pend, "period_type": ptype})
        rows = res.get("observations", [])
        if not rows:
            return self._resp(case_id, "insufficient_evidence", "No reference observation available for this period.", 0.85,
                              f"The reference store has no {concept} observation for {company['ticker']} period ending {pend}; the record cannot be verified.",
                              "Request the source filing or abstain from using this value.",
                              issues=["missing_input"], company=cik10, period=pend, evidence=ev)
        ref = rows[0]
        ev.append(EvidenceItem(source="reference_store", ref=ref["ref"], value=ref["value"],
                               detail=f"SEC {ref['form']} {ref['accession_number']} filed {ref['filing_date']}"))
        if rec.get("value") is None:
            return self._resp(case_id, "flagged", "Record is missing its value; reference value retrieved.", 0.8,
                              "The required value field is absent. The verified value was retrieved from the reference store.",
                              f"Backfill from SEC filing {ref['accession_number']} after review.", issues=["missing_input"],
                              value=ref["value"], company=cik10, period=pend, evidence=ev)
        cmp = store.call("compare_values", {"value_a": rec["value"], "unit_a": rec.get("unit"),
                                            "value_b": ref["value"], "unit_b": ref["unit"], "tolerance": TOL})
        if "error" in cmp:
            return self._resp(case_id, "flagged", "Unit could not be normalized.", 0.7, f"Unit problem: {cmp['error']}.",
                              "Correct the unit label and re-verify.", issues=["unit_inconsistency"], value=ref["value"],
                              company=cik10, period=pend, evidence=ev)
        if cmp["within_tolerance"]:
            return self._resp(case_id, "verified", "Value matches the reference observation.", 0.97,
                              f"After unit normalization the value equals the SEC-reported {concept} for the period within 0.5%.",
                              "No action required.", value=ref["value"], company=cik10, period=pend, evidence=ev)
        if cmp["power_of_ten"]:
            if self.check_scale:
                return self._resp(case_id, "flagged", "Scale error detected.", 0.93,
                                  f"The record differs from the reference by a factor of 10^{cmp['power_of_ten']}; likely a thousands/millions confusion.",
                                  "Correct the scale or unit label and use the reference value.", issues=["scale_mismatch"],
                                  value=ref["value"], company=cik10, period=pend, evidence=ev)
            return self._resp(case_id, "verified", "Value accepted.", 0.92, "Value looks consistent in magnitude.",
                              "No action required.", value=rec["value"], company=cik10, period=pend, evidence=ev)
        other = store.call("get_observations", {"company": cik10, "concept": concept, "period_type": ptype}).get("observations", [])
        match = [o for o in other if o["period_end"] != pend and within_tolerance(cmp["a_base"], o["value"], TOL)]
        if match:
            if self.check_period:
                ev.append(EvidenceItem(source="reference_store", ref=match[0]["ref"], value=match[0]["value"],
                                       detail=f"same value is reported for period ending {match[0]['period_end']}"))
                return self._resp(case_id, "flagged", "Reporting-period mismatch detected.", 0.9,
                                  f"The stated value equals the {concept} for the period ending {match[0]['period_end']}, not {pend}.",
                                  "Correct the fiscal period before use.", issues=["period_mismatch"], value=ref["value"],
                                  company=cik10, period=pend, evidence=ev)
            return self._resp(case_id, "verified", "Value accepted.", 0.9, "A matching value exists for this company and concept.",
                              "No action required.", value=rec["value"], company=cik10, period=pend, evidence=ev)
        return self._resp(case_id, "flagged", "Value does not match the reference.", 0.8,
                          "The value differs from the SEC-reported figure and matches no other period or scale.",
                          "Investigate the source of the figure.", issues=["value_mismatch"], value=ref["value"],
                          company=cik10, period=pend, evidence=ev)

    # ---- multiple records for the same fact -------------------------------
    def _multi(self, case_id, recs, store) -> AgentResponse:
        ev = [EvidenceItem(source="input_record", ref=r["record_id"], value=r.get("value"), detail=f"{r.get('source_label')} says {r.get('value')} {r.get('unit')}") for r in recs]
        company, problems, id_ev = self._identity(recs[0], store)
        ev += id_ev
        if company is None or problems:
            return self._resp(case_id, "ambiguous", "Company identity could not be established.", 0.5,
                              "; ".join(problems) or "The company could not be resolved.", "Escalate for manual review.",
                              issues=["identifier_collision"], evidence=ev)
        cik10 = pad_cik(company["cik"])
        r0 = recs[0]
        base0 = None
        bases: list[float] = []
        for r in recs:
            c = store.call("compare_values", {"value_a": r["value"], "unit_a": r.get("unit"),
                                              "value_b": recs[0]["value"], "unit_b": recs[0].get("unit"), "tolerance": TOL})
            if "error" in c:
                return self._resp(case_id, "flagged", "Unit could not be normalized.", 0.7, c["error"], "Correct the unit labels.",
                                  issues=["unit_inconsistency"], company=cik10, evidence=ev)
            bases.append(c["a_base"])
            base0 = c["b_base"]
        all_equal = all(within_tolerance(b, base0, TOL) for b in bases)
        units_differ = len({r.get("unit") for r in recs}) > 1
        res = store.call("get_observations", {"company": cik10, "concept": r0["concept"],
                                              "period_end": r0["fiscal_period_end"], "period_type": r0.get("period_type", "annual")})
        ref = (res.get("observations") or [None])[0]
        if ref:
            ev.append(EvidenceItem(source="reference_store", ref=ref["ref"], value=ref["value"],
                                   detail=f"SEC {ref['form']} {ref['accession_number']}"))
        if all_equal and units_differ:
            return self._resp(case_id, "flagged", "Feeds use inconsistent unit labels.", 0.88,
                              "The feeds agree after unit normalization but label the same value with different units.",
                              "Standardize unit labels at the source.", issues=["unit_inconsistency"],
                              value=ref["value"] if ref else base0, company=cik10, period=r0["fiscal_period_end"], evidence=ev)
        if all_equal:
            return self._resp(case_id, "verified", "Feeds agree.", 0.9, "All records agree after normalization.", "No action required.",
                              value=ref["value"] if ref else base0, company=cik10, period=r0["fiscal_period_end"], evidence=ev)
        if ref and any(within_tolerance(b, ref["value"], TOL) for b in bases):
            return self._resp(case_id, "flagged", "Feeds conflict; reference resolves it.", 0.85,
                              "The feeds disagree; the reference store matches one of them.", "Reject the non-matching feed.",
                              issues=["conflicting_sources"], value=ref["value"], company=cik10,
                              period=r0["fiscal_period_end"], evidence=ev)
        return self._resp(case_id, "ambiguous", "Irreconcilable conflict between sources.", 0.6,
                          "Two sources report different values for the same company, concept and period, and the reference store cannot adjudicate.",
                          "Escalate to a data steward; do not publish either value.", issues=["conflicting_sources"],
                          company=cik10, period=r0["fiscal_period_end"], evidence=ev)
