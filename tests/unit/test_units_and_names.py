import pytest

from src.identity.resolver import name_similarity, normalize_name, resolve_entity
from src.normalization.units import UnsupportedUnit, power_of_ten_ratio, to_base, within_tolerance


def test_to_base_scales():
    assert to_base(5, "USD") == (5.0, "USD")
    assert to_base(5, "USDthousands") == (5000.0, "USD")
    assert to_base(2, "USDmillions") == (2_000_000.0, "USD")


@pytest.mark.parametrize("unit", [None, "", "EUR", "GBP", "shares"])
def test_to_base_rejects_unsupported(unit):
    with pytest.raises(UnsupportedUnit):
        to_base(1, unit)


def test_tolerance_cannot_hide_scale_error():
    assert within_tolerance(1004, 1000, 0.005) is True
    assert within_tolerance(1_000_000, 1000, 0.005) is False
    assert power_of_ten_ratio(1_000_000, 1000) == 3
    assert power_of_ten_ratio(1000, 1_000_000) == -3
    assert power_of_ten_ratio(1100, 1000) is None


def test_name_normalization_and_similarity():
    assert normalize_name("The Coca-Cola Company") == "coca cola"
    assert normalize_name("Johnson & Johnson") == "johnson and johnson"
    assert name_similarity("Apple Inc.", "APPLE INC") == 1.0
    assert name_similarity("Alpha Test Corp", "Bravo Test Corp") < 0.5


class C:
    def __init__(self, lei, name, jur="US-CA", status="ACTIVE"):
        self.lei, self.legal_name, self.jurisdiction, self.entity_status = lei, name, jur, status


def test_resolver_unique_ambiguous_nomatch():
    assert resolve_entity("Apple Inc.", [C("L1", "APPLE INC.")]).status == "CANDIDATE_UNIQUE"
    amb = resolve_entity("Apple Inc.", [C("L1", "Apple Inc.", "DE-X"), C("L2", "Apple Inc", "FR-X")])
    assert amb.status == "AMBIGUOUS" and amb.selected_lei is None
    disamb = resolve_entity("Apple Inc.", [C("L1", "Apple Inc.", "US-CA"), C("L2", "Apple Inc", "FR-X")])
    assert disamb.status == "CANDIDATE_UNIQUE" and disamb.selected_lei == "L1"
    fuzzy = resolve_entity("Apple Computer Inc", [C("L3", "Apple Computer Holdings")])  # similarity 0.67, not exact
    assert fuzzy.status == "AMBIGUOUS" and fuzzy.selected_lei is None
    assert resolve_entity("Apple Inc.", [C("L4", "Zebra Mining")]).status == "UNMATCHED"
    assert resolve_entity("Apple Inc.", [C("L5", "Apple Inc.", status="INACTIVE")]).status == "AMBIGUOUS"  # inactive exact name: candidate, never auto-selected
