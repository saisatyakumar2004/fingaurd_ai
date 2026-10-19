"""Unit-conversion layer (separate from storage; stored values keep their reported unit).

Supported: USD and explicit scale labels (USDthousands / USDmillions / USDbillions).
Any other currency raises UnsupportedUnit -- we do NOT guess FX rates, and a tolerance
check must never be used to paper over a unit or currency mismatch.
"""
from __future__ import annotations

import math
import re

SCALE_UNITS: dict[str, float] = {
    "USD": 1.0,
    "USDthousands": 1e3,
    "USDmillions": 1e6,
    "USDbillions": 1e9,
}


class UnsupportedUnit(ValueError):
    pass


def is_currency_code(unit: str | None) -> bool:
    return bool(unit) and re.fullmatch(r"[A-Za-z]{3}", unit) is not None


def to_base(value: float, unit: str | None) -> tuple[float, str]:
    """Convert (value, unit) to (value_in_USD, 'USD')."""
    if unit is None or unit == "":
        raise UnsupportedUnit("missing unit")
    if unit in SCALE_UNITS:
        return float(value) * SCALE_UNITS[unit], "USD"
    if is_currency_code(unit):
        raise UnsupportedUnit(f"no FX conversion is supported for currency {unit}")
    raise UnsupportedUnit(f"unrecognised unit {unit!r}")


def within_tolerance(actual: float | None, expected: float | None, rel_tol: float) -> bool:
    if actual is None or expected is None:
        return False
    if math.isnan(actual) or math.isnan(expected):
        return False
    if expected == 0:
        return actual == 0
    return abs(actual - expected) <= rel_tol * abs(expected)


def power_of_ten_ratio(a: float | None, b: float | None, rel_tol: float = 1e-3) -> int | None:
    """Return k (a multiple of 3, k != 0) when a ~= b * 10**k, else None.
    Used to recognise thousands/millions/billions scale confusions."""
    if a is None or b is None or a == 0 or b == 0:
        return None
    if (a > 0) != (b > 0):
        return None
    k = round(math.log10(abs(a / b)))
    if k == 0 or k % 3 != 0:
        return None
    if abs(a / (b * (10.0 ** k)) - 1.0) <= rel_tol:
        return int(k)
    return None
