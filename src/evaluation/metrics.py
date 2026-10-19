"""Aggregate metrics computed purely from per-case results (no I/O, independently testable)."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

DEFAULT_WEIGHTS = {"critical": 10, "high": 5, "medium": 3, "low": 1}
ATTRIBUTION_RISK = {"ID_MISMAP", "PERIOD_SHIFT"}   # wrong company / wrong period


def _div(a: float, b: float) -> float | None:
    return a / b if b else None


def calibration(confidences: list[float], outcomes: list[bool], n_bins: int = 10) -> dict[str, Any]:
    n = len(confidences)
    bins = [{"lo": i / n_bins, "hi": (i + 1) / n_bins, "n": 0, "conf_sum": 0.0, "correct": 0} for i in range(n_bins)]
    for c, o in zip(confidences, outcomes):
        b = bins[min(int(c * n_bins), n_bins - 1)]
        b["n"] += 1
        b["conf_sum"] += c
        b["correct"] += 1 if o else 0
    out_bins, ece = [], 0.0
    for b in bins:
        if b["n"]:
            mc, acc = b["conf_sum"] / b["n"], b["correct"] / b["n"]
            ece += b["n"] / n * abs(acc - mc)
        else:
            mc = acc = None
        out_bins.append({"lo": b["lo"], "hi": b["hi"], "n": b["n"], "mean_confidence": mc, "accuracy": acc})
    brier = sum((c - (1.0 if o else 0.0)) ** 2 for c, o in zip(confidences, outcomes)) / n if n else None
    return {"n": n, "bins": out_bins, "brier": brier, "ece": ece if n else None,
            "note": "Agent confidence is self-reported and NOT a statistically validated probability."}


def aggregate(results: Iterable[dict[str, Any]], high_conf_threshold: float = 0.8, n_bins: int = 10,
              weights: dict[str, int] | None = None) -> dict[str, Any]:
    weights = weights or DEFAULT_WEIGHTS
    res = list(results)
    total = len(res)
    passed = sum(1 for r in res if r["score"]["passed"])

    by_cat: dict[str, dict[str, Any]] = {}
    for cat in sorted({r["category"] for r in res}):
        g = [r for r in res if r["category"] == cat]
        p = sum(1 for r in g if r["score"]["passed"])
        by_cat[cat] = {"n": len(g), "passed": p, "accuracy": _div(p, len(g)),
                       # lenient companion metric: right STATUS regardless of issue labels / numbers (strict `accuracy` stays primary)
                       "status_accuracy": _div(sum(1 for r in g if r["score"].get("status_ok")), len(g))}

    pos = [r for r in res if r["expected"]["is_error"]]
    neg = [r for r in res if not r["expected"]["is_error"]]
    tp = sum(1 for r in pos if r["score"]["predicted_issue"])
    fn = len(pos) - tp
    fp = sum(1 for r in neg if r["score"]["predicted_issue"])
    tn = len(neg) - fp
    precision, recall = _div(tp, tp + fp), _div(tp, tp + fn)
    f1 = _div(2 * precision * recall, precision + recall) if precision is not None and recall is not None else None

    # per-category detection: that category's error cases are the positives, ALL clean controls the negatives
    for cat, v in by_cat.items():
        if cat in {r["category"] for r in pos}:
            g = [r for r in pos if r["category"] == cat]
            ctp = sum(1 for r in g if r["score"]["predicted_issue"])
            cp, cr = _div(ctp, ctp + fp), _div(ctp, len(g))
            v.update({"detection_precision": cp, "detection_recall": cr,
                      "detection_f1": _div(2 * cp * cr, cp + cr) if cp is not None and cr is not None else None})
        else:
            v.update({"detection_precision": None, "detection_recall": None, "detection_f1": None})

    should = [r for r in res if r["score"]["abstain_expected"]]
    abstained = [r for r in res if r["score"]["abstained"]]
    adequate = [r for r in res if not r["score"]["abstain_expected"]]
    abstention = {
        "cases_requiring_abstention": len(should),
        "abstention_recall": _div(sum(1 for r in should if r["score"]["abstained"]), len(should)),
        "abstention_precision": _div(sum(1 for r in abstained if r["score"]["abstain_expected"]), len(abstained)),
        "answered_correctly_when_evidence_adequate": _div(sum(1 for r in adequate if r["score"]["passed"]), len(adequate)),
        "incorrect_abstention_rate": _div(sum(1 for r in adequate if r["score"]["abstained"]), len(adequate)),
        "invented_values_when_abstention_required": sum(1 for r in should if r["score"]["invented_value"]),
    }

    confs = [float((r.get("response") or {}).get("confidence", 0.0)) for r in res]
    outcomes = [bool(r["score"]["passed"]) for r in res]
    cal = calibration(confs, outcomes, n_bins)
    hc_wrong = [
        {"case_id": r["case_id"], "category": r["category"], "severity": r["severity"],
         "confidence": c, "status": (r.get("response") or {}).get("status")}
        for r, c in zip(res, confs) if c >= high_conf_threshold and not r["score"]["passed"]
    ]

    rub = defaultdict(list)
    for r in res:
        for k, v in r["score"]["rubric"].items():
            rub[k].append(v)
    rubric = {k: sum(v) / len(v) for k, v in rub.items()}

    fails = [(r, c) for r, c in zip(res, confs) if not r["score"]["passed"]]
    cause_count: Counter[str] = Counter()
    cause_score: dict[str, float] = defaultdict(float)
    sev_count: Counter[str] = Counter()
    ranked = []
    for r, c in fails:
        cause = r.get("root_cause") or "unclassified"
        pri = weights.get(r["severity"], 1) * (1.0 + c)
        cause_count[cause] += 1
        cause_score[cause] += pri
        sev_count[r["severity"]] += 1
        ranked.append({"case_id": r["case_id"], "category": r["category"], "subtype": r.get("subtype"),
                       "severity": r["severity"], "root_cause": cause, "confidence": c, "priority": round(pri, 3),
                       "wrong_company_or_period_risk": r["category"] in ATTRIBUTION_RISK})
    ranked.sort(key=lambda x: (-x["wrong_company_or_period_risk"], -x["priority"], x["case_id"]))
    causes = sorted(({"cause": k, "count": v, "weighted_score": round(cause_score[k], 3)} for k, v in cause_count.items()),
                    key=lambda x: (-x["weighted_score"], x["cause"]))

    return {
        "total": total, "passed": passed, "accuracy": _div(passed, total),
        "status_accuracy": _div(sum(1 for r in res if r["score"].get("status_ok")), total),
        "by_category": by_cat,
        "detection": {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall, "f1": f1},
        "false_positive_rate": _div(fp, fp + tn),
        "abstention": abstention,
        "calibration": cal,
        "high_confidence_wrong": {"threshold": high_conf_threshold, "count": len(hc_wrong), "cases": hc_wrong},
        "rubric": rubric,
        "evidence_completeness": _div(sum(1 for r in res if r["score"]["evidence_ok"]), total),
        "numeric_accuracy": _div(sum(1 for r in res if r["score"].get("numeric_ok") is True), sum(1 for r in res if r["score"].get("numeric_ok") is not None)),
        "identity_accuracy": _div(sum(1 for r in res if r["score"].get("identity_ok") is True), sum(1 for r in res if r["score"].get("identity_ok") is not None)),
        "root_causes": causes,
        "severity": {"failures_by_severity": dict(sev_count),
                     "weighted_failure_score": round(sum(cause_score.values()), 3)},
        "ranked_failures": ranked,
    }


def cohens_kappa(a: list[str], b: list[str]) -> float | None:
    """Agreement between two label sequences (e.g. LLM judge vs human labels)."""
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)
