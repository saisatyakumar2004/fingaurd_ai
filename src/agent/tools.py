"""Controlled, case-scoped reference store exposed to the agent as tools.

The store reads golden SEC-derived observations from DuckDB. For a given case it may hide
specific periods (`exclude_keys`, from the hidden store_overrides) to simulate missing
reference data. The agent never receives the overrides, expected answers or labels.
"""
from __future__ import annotations

from typing import Any

from src.common.db import fetch_dicts
from src.common.util import cik_int, pad_cik
from src.identity.resolver import normalize_name
from src.normalization.units import UnsupportedUnit, power_of_ten_ratio, to_base, within_tolerance

TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "lookup_company", "description": "Resolve a company by CIK, ticker or exact (normalized) name.",
     "args": {"identifier": "string"}},
    {"name": "get_observations",
     "description": "Verified SEC observations for a company+concept. Omit period_end to list all periods.",
     "args": {"company": "CIK or ticker", "concept": "revenue|net_income|total_assets",
              "period_end": "YYYY-MM-DD (optional)", "period_type": "annual|quarterly (default annual)"}},
    {"name": "get_provenance", "description": "Source snapshot details (url, hash, retrieval time) for an observation ref.",
     "args": {"observation_id": "string"}},
    {"name": "compare_values",
     "description": "Normalize two (value, unit) pairs to USD and report relative difference and power-of-ten scale ratio.",
     "args": {"value_a": "number", "unit_a": "string", "value_b": "number", "unit_b": "string"}},
    {"name": "lookup_instrument", "description": "Resolve a FIGI (or ticker) to the owning company.",
     "args": {"figi": "string (optional)", "ticker": "string (optional)"}},
]


def _iso(d: Any) -> Any:
    return d.isoformat() if hasattr(d, "isoformat") else d


class ReferenceStore:
    def __init__(self, con, exclude_keys: list[dict[str, str]] | None = None) -> None:
        self.con = con
        self.exclude = {(k["company_id"], k["concept"], k["period_type"], k["period_end"]) for k in (exclude_keys or [])}
        self.trace: list[dict[str, Any]] = []
        self.issued: dict[str, Any] = {}       # ref -> value shown to the agent (None for non-numeric refs)

    # ---- dispatch -----------------------------------------------------
    def call(self, tool: str, args: dict[str, Any] | None) -> dict[str, Any]:
        args = args or {}
        if tool not in {s["name"] for s in TOOL_SPECS}:
            res: dict[str, Any] = {"error": f"unknown tool {tool!r}"}
        else:
            try:
                res = getattr(self, tool)(**args)
            except TypeError as e:
                res = {"error": f"bad arguments: {e}"}
            except Exception as e:  # tool failures must be visible to the agent, not crash the run
                res = {"error": f"{type(e).__name__}: {e}"}
        self.trace.append({"tool": tool, "args": args, "ok": "error" not in res})
        return res

    # ---- tools --------------------------------------------------------
    def _find_companies(self, identifier: str) -> list[dict[str, Any]]:
        ident = str(identifier).strip()
        rows: list[dict[str, Any]] = []
        if ident.isdigit() or ident.upper().startswith("CIK"):
            n = cik_int(ident)
            if n is not None:
                rows = fetch_dicts(self.con, "SELECT * FROM companies WHERE cik = ?", [n])
        if not rows:
            rows = fetch_dicts(self.con, "SELECT * FROM companies WHERE upper(ticker) = upper(?)", [ident])
        if not rows:
            target = normalize_name(ident)
            if target:
                rows = [c for c in fetch_dicts(self.con, "SELECT * FROM companies")
                        if target in (normalize_name(c["name"]), normalize_name(c["sec_entity_name"]))]
        return rows

    @staticmethod
    def _company_view(c: dict[str, Any]) -> dict[str, Any]:
        return {"company_id": c["company_id"], "cik": pad_cik(c["cik"]), "ticker": c["ticker"],
                "name": c["name"], "sec_entity_name": c["sec_entity_name"], "sector": c["sector"]}

    def lookup_company(self, identifier: str) -> dict[str, Any]:
        rows = self._find_companies(identifier)
        if not rows:
            return {"found": False, "ambiguous": False, "company": None}
        views = [self._company_view(c) for c in rows]
        for v in views:
            self.issued[v["company_id"]] = None
        return {"found": True, "ambiguous": len(views) > 1, "company": views[0], "matches": views}

    def get_observations(self, company: str, concept: str, period_end: str | None = None,
                         period_type: str = "annual") -> dict[str, Any]:
        rows = self._find_companies(company)
        if len(rows) != 1:
            return {"found": False, "observations": [], "note": "company not found or ambiguous"}
        comp = rows[0]
        obs_rows = fetch_dicts(
            self.con, "SELECT * FROM financial_observations WHERE company_id = ? AND concept = ? AND period_type = ? "
                      "ORDER BY fiscal_period_end, filing_date, accession_number", [comp["company_id"], concept, period_type])
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in obs_rows:
            end = r["fiscal_period_end"].isoformat()
            if (comp["company_id"], concept, period_type, end) in self.exclude:
                continue
            if period_end and end != period_end:
                continue
            groups.setdefault(end, []).append(r)
        out = []
        for end, g in sorted(groups.items()):
            sel = next((x for x in g if x["is_selected"]), g[-1])
            alts = [{"accession_number": x["accession_number"], "value": x["value"], "filing_date": _iso(x["filing_date"])}
                    for x in g if x is not sel]
            out.append({
                "ref": sel["observation_id"], "period_end": end, "period_type": period_type,
                "value": sel["value"], "unit": sel["unit"], "filing_date": _iso(sel["filing_date"]),
                "accession_number": sel["accession_number"], "form": sel["form"], "xbrl_tag": sel["xbrl_tag"],
                "source_url": sel["source_url"], "alternate_reports": alts,
            })
            self.issued[sel["observation_id"]] = sel["value"]
        return {"found": bool(out), "company_cik": pad_cik(comp["cik"]), "observations": out}

    def get_provenance(self, observation_id: str) -> dict[str, Any]:
        rows = fetch_dicts(
            self.con, "SELECT o.observation_id, o.company_id, o.concept, o.period_type, o.fiscal_period_end, o.accession_number, "
                      "o.form, o.filing_date, o.xbrl_tag, o.source_url, s.sha256, s.retrieval_timestamp, s.snapshot_id "
                      "FROM financial_observations o LEFT JOIN source_snapshots s ON s.snapshot_id = o.snapshot_id "
                      "WHERE o.observation_id = ?", [observation_id])
        if not rows:
            return {"found": False}
        r = rows[0]
        if (r["company_id"], r["concept"], r["period_type"], r["fiscal_period_end"].isoformat()) in self.exclude:
            return {"found": False}
        return {"found": True, **{k: _iso(v) if k != "retrieval_timestamp" else str(v) for k, v in r.items()}}

    def compare_values(self, value_a: float, unit_a: str, value_b: float, unit_b: str,
                       tolerance: float = 0.005) -> dict[str, Any]:
        try:
            a, _ = to_base(float(value_a), unit_a)
            b, _ = to_base(float(value_b), unit_b)
        except (UnsupportedUnit, TypeError, ValueError) as e:
            return {"error": str(e)}
        return {"a_base": a, "b_base": b, "relative_difference": abs(a - b) / abs(b) if b else None,
                "within_tolerance": within_tolerance(a, b, tolerance), "power_of_ten": power_of_ten_ratio(a, b)}

    def lookup_instrument(self, figi: str | None = None, ticker: str | None = None) -> dict[str, Any]:
        if not figi and not ticker:
            return {"error": "provide figi or ticker"}
        if figi:
            rows = fetch_dicts(
                self.con, "SELECT m.figi, m.composite_figi, m.share_class_figi, m.security_type2, c.cik, c.ticker, c.company_id "
                          "FROM instrument_mappings m JOIN companies c ON c.company_id = m.company_id "
                          "WHERE m.figi = ? OR m.composite_figi = ? OR m.share_class_figi = ?", [figi, figi, figi])
            if not rows:  # synthetic/fixture stores register FIGIs only as identifiers
                rows = fetch_dicts(
                    self.con, "SELECT i.id_value AS figi, NULL AS composite_figi, NULL AS share_class_figi, NULL AS security_type2, "
                              "c.cik, c.ticker, c.company_id FROM company_identifiers i JOIN companies c ON c.company_id = i.company_id "
                              "WHERE i.id_value = ? AND i.id_type IN ('FIGI','COMPOSITE_FIGI','SHARE_CLASS_FIGI')", [figi])
        else:
            rows = fetch_dicts(
                self.con, "SELECT m.figi, m.composite_figi, m.share_class_figi, m.security_type2, c.cik, c.ticker, c.company_id "
                          "FROM instrument_mappings m JOIN companies c ON c.company_id = m.company_id "
                          "WHERE upper(m.ticker) = upper(?)", [ticker])
        matches = [{"figi": r["figi"], "composite_figi": r["composite_figi"], "share_class_figi": r["share_class_figi"],
                    "company_cik": pad_cik(r["cik"]), "company_ticker": r["ticker"], "security_type2": r["security_type2"]}
                   for r in rows]
        for r in rows:
            self.issued[figi or r["figi"]] = None
            self.issued[r["company_id"]] = None
        return {"found": bool(matches), "matches": matches}
