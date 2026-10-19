"""Entity name matching: candidate generation, NOT proof of identity.

The resolver keeps the query, every candidate with its score, and an explicit status.
If more than one plausible entity remains it reports `ambiguous` and selects nothing.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc", "lp", "the"}


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    s = s.replace("&", " and ").replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(t for t in s.split() if t not in SUFFIXES)


def name_similarity(a: str | None, b: str | None) -> float:
    ta, tb = set(normalize_name(a).split()), set(normalize_name(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Resolution:
    status: str                     # VERIFIED | CANDIDATE_UNIQUE | AMBIGUOUS | UNMATCHED | SOURCE_UNAVAILABLE
    selected_lei: str | None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""


def resolve_entity(query: str, candidates: list[Any], us_prefix: str = "US", expected_lei: str | None = None,
                   sec_profile: dict[str, Any] | None = None) -> Resolution:
    """`candidates` are objects/dicts with lei, legal_name, jurisdiction, entity_status.

    Status semantics (a name match alone is never proof):
      VERIFIED          single exact-name ACTIVE candidate AND independent evidence agrees: either an expected_lei supplied
                        from another source, or the SEC submissions profile (incorporation state AND business city/state) matches
                        the candidate's GLEIF jurisdiction and address. With several exact-name candidates, exactly one must be
                        corroborated.
      CANDIDATE_UNIQUE  single exact-name ACTIVE candidate, no independent corroboration
      AMBIGUOUS         several plausible candidates, only fuzzy candidates, or expected_lei contradicts the name match
      UNMATCHED         nothing plausible
    SOURCE_UNAVAILABLE is assigned by the ingestion layer when the API cannot be reached.
    """
    scored: list[dict[str, Any]] = []
    for c in candidates:
        get = (lambda k: c[k]) if isinstance(c, dict) else (lambda k: getattr(c, k))
        scored.append({
            "lei": get("lei"),
            "legal_name": get("legal_name"),
            "jurisdiction": get("jurisdiction"),
            "entity_status": get("entity_status"),
            "similarity": round(name_similarity(query, get("legal_name")), 3),
            "exact_name": normalize_name(query) == normalize_name(get("legal_name")),
            "legal_city": getattr(c, "legal_city", None) if not isinstance(c, dict) else c.get("legal_city"),
            "hq_city": getattr(c, "hq_city", None) if not isinstance(c, dict) else c.get("hq_city"),
            "hq_region": getattr(c, "hq_region", None) if not isinstance(c, dict) else c.get("hq_region"),
        })
    active_exact = [s for s in scored if s["exact_name"] and (s["entity_status"] or "").upper() == "ACTIVE"]
    if expected_lei and scored and not any(s["lei"] == expected_lei for s in active_exact):
        return Resolution("AMBIGUOUS", None, scored, f"expected LEI {expected_lei} is not among exact-name ACTIVE candidates (possible incorrect mapping)")
    if sec_profile and active_exact and not expected_lei:
        for sc in active_exact:
            sc["corroboration"] = corroborate(sc, sec_profile)
        ok = [sc for sc in active_exact if sc["corroboration"]["ok"]]
        if len(ok) == 1:
            return Resolution("VERIFIED", ok[0]["lei"], scored,
                              "exact-name ACTIVE candidate corroborated by SEC submissions: " + "; ".join(ok[0]["corroboration"]["reasons"]))
        if len(ok) > 1:
            return Resolution("AMBIGUOUS", None, scored, f"{len(ok)} candidates match SEC jurisdiction and address")
        if len(active_exact) == 1 and active_exact[0]["corroboration"]["contradicts"]:
            return Resolution("AMBIGUOUS", None, scored, "single exact-name candidate contradicts SEC profile: " +
                              "; ".join(active_exact[0]["corroboration"]["reasons"]))
        if len(active_exact) > 1:
            return Resolution("AMBIGUOUS", None, scored, f"{len(active_exact)} exact-name ACTIVE candidates and none corroborated by SEC profile")
    if len(active_exact) == 1:
        if expected_lei:
            return Resolution("VERIFIED", active_exact[0]["lei"], scored, "exact-name match corroborated by expected LEI")
        return Resolution("CANDIDATE_UNIQUE", active_exact[0]["lei"], scored, "single ACTIVE candidate with identical normalized name (name-only evidence)")
    if len(active_exact) > 1:
        us = [s for s in active_exact if (s["jurisdiction"] or "").upper().startswith(us_prefix)]
        if len(us) == 1:
            return Resolution("VERIFIED" if expected_lei else "CANDIDATE_UNIQUE", us[0]["lei"], scored,
                              f"{len(active_exact)} exact-name ACTIVE candidates; one has a {us_prefix} jurisdiction")
        return Resolution("AMBIGUOUS", None, scored, f"{len(active_exact)} exact-name ACTIVE candidates; not disambiguated")
    near = [s for s in scored if s["similarity"] >= 0.6]
    if near:
        return Resolution("AMBIGUOUS", None, scored, "only fuzzy name candidates; none selected")
    return Resolution("UNMATCHED", None, scored, "no plausible candidate")


def corroborate(candidate: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Independent corroboration of a GLEIF candidate with the SEC submissions profile.
    ok            = incorporation state matches the GLEIF jurisdiction AND the SEC business city/state matches a GLEIF address
    contradicts   = both sides state a US jurisdiction/state and they differ
    insufficient data on either side never produces ok."""
    reasons: list[str] = []
    state, juris = (profile.get("state_of_incorporation") or "").upper(), (candidate.get("jurisdiction") or "").upper()
    j_ok = None
    if len(state) == 2 and state.isalpha() and juris:
        j_ok = juris == f"US-{state}"
        reasons.append(f"jurisdiction {juris} {'==' if j_ok else '!='} SEC state of incorporation {state}")
    city = (profile.get("business_city") or "").strip().upper()
    bstate = (profile.get("business_state") or "").strip().upper()
    cand_cities = {(candidate.get(k) or "").strip().upper() for k in ("hq_city", "legal_city")} - {""}
    a_ok = None
    if city and cand_cities:
        a_ok = city in cand_cities
        if a_ok and candidate.get("hq_region") and bstate:
            a_ok = candidate["hq_region"].upper().endswith(bstate)
        reasons.append(f"SEC business city {city} {'found in' if a_ok else 'not in'} GLEIF addresses {sorted(cand_cities)}")
    return {"ok": bool(j_ok and a_ok), "contradicts": j_ok is False, "reasons": reasons}
