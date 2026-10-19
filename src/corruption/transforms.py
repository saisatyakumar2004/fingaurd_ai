"""Reversible record transforms. Each `apply_*` has an exact `reverse_*`.

Transforms operate on COPIES of presented records. Source snapshots and golden records in the
database are never touched; corrupted records only exist inside generated test cases.
"""
from __future__ import annotations

import copy
from typing import Any, Callable


def apply_scale_value(rec: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    r = copy.deepcopy(rec)
    r["value"] = rec["value"] * p["factor"] if p["op"] == "mul" else rec["value"] / p["factor"]
    return r


def reverse_scale_value(rec: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    r = copy.deepcopy(rec)
    r["value"] = rec["value"] / p["factor"] if p["op"] == "mul" else rec["value"] * p["factor"]
    return r


def apply_set_field(rec: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    r = copy.deepcopy(rec)
    r[p["field"]] = p["to"]
    return r


def reverse_set_field(rec: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    r = copy.deepcopy(rec)
    r[p["field"]] = p["from"]
    return r


def apply_delete_field(rec: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    r = copy.deepcopy(rec)
    r.pop(p["field"], None)
    return r


def reverse_delete_field(rec: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    r = copy.deepcopy(rec)
    r[p["field"]] = p["original"]
    return r


TRANSFORMS: dict[str, tuple[Callable, Callable]] = {
    "scale_value": (apply_scale_value, reverse_scale_value),
    "set_field": (apply_set_field, reverse_set_field),
    "delete_field": (apply_delete_field, reverse_delete_field),
}


def apply_steps(rec: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(rec)
    for s in steps:
        out = TRANSFORMS[s["transform"]][0](out, s["params"])
    return out


def reverse_steps(rec: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(rec)
    for s in reversed(steps):
        out = TRANSFORMS[s["transform"]][1](out, s["params"])
    return out
