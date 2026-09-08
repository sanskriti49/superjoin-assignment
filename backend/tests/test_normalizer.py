"""Normalization rules, including the ones that were previously wrong."""

import pytest

from app.pipeline.normalizer import FactNormalizer as N


class TestQuantities:
    def test_indian_and_western_scales_meet(self):
        _, unit_a, value_a = N.parse_numeric_value("₹8,142 Cr")
        _, unit_b, value_b = N.parse_numeric_value("₹81,415 Mn")
        assert unit_a == unit_b == "INR"
        assert abs(value_a - value_b) / value_a < 0.001

    def test_accounting_parentheses_mean_negative(self):
        mantissa, _, value = N.parse_numeric_value("₹(452) Cr")
        assert mantissa == -452.0
        assert value == pytest.approx(-4.52e9)

    def test_parentheses_around_words_do_not_flip_the_sign(self):
        # A trailing gloss must not turn a positive figure negative.
        mantissa, unit, value = N.parse_numeric_value("6.5 per cent (of GDP)")
        assert mantissa == 6.5
        assert value == 6.5
        assert unit == "% of GDP"

    def test_percentages_are_never_scaled(self):
        assert N.parse_numeric_value("6.5 per cent")[2] == 6.5

    def test_magnitude_is_read_from_the_words_next_to_the_number(self):
        # "578 Cr to Rs. 127 Cr" must not let a later word set the scale.
        assert N.parse_numeric_value("578 Cr")[2] == pytest.approx(5.78e9)

    @pytest.mark.parametrize("text,expected", [
        ("US$ 668.3 billion", "USD"),
        ("$668.3 billion", "USD"),
        ("Rs. 578 Cr", "INR"),
        ("740 Mn shipments", "shipments"),
        ("25 bps", "bps"),
    ])
    def test_units_are_detected(self, text, expected):
        assert N.parse_numeric_value(text)[1] == expected


class TestPeriods:
    @pytest.mark.parametrize("text,expected", [
        ("FY24", "2023-2024"),
        ("FY 2024-25", "2024-2025"),
        ("2024-25", "2024-2025"),
        ("as at end-March 2025", "2025-03-31"),
        ("end-December 2024", "2024-12-31"),
        ("Q4 FY24", "2024-Q4"),
        ("31 July 2023", "2023-07-31"),
        ("September 30, 2019", "2019-09-30"),
        ("2019", "2019"),
    ])
    def test_periods_normalize(self, text, expected):
        assert N.normalize_time_period(text) == expected

    def test_any_month_parses_not_just_the_ones_in_the_starter_documents(self):
        assert N.normalize_time_period("July 2023") == "2023-07-31"
        assert N.normalize_time_period("end-February 2024") == "2024-02-29"

    def test_unparseable_period_is_none_not_a_guess(self):
        assert N.normalize_time_period("some time later") is None

    def test_containment_is_not_treated_as_a_match(self):
        # A fiscal year and a date inside it are different claims.
        assert not N.periods_overlap("2024-2025", "2025-03-31")


class TestUnits:
    def test_different_currencies_are_not_comparable(self):
        assert not N.units_comparable("USD", "INR")

    def test_a_currency_and_a_percentage_are_not_comparable(self):
        assert not N.units_comparable("USD", "%")

    def test_the_same_currency_is_comparable(self):
        assert N.units_comparable("INR", "INR")


class TestNames:
    def test_legal_suffixes_collapse(self):
        assert (N.normalize_entity("Delhivery Limited")[1]
                == N.normalize_entity("Delhivery Ltd.")[1]
                == N.normalize_entity("Delhivery")[1])

    def test_possessives_are_stripped(self):
        assert N.normalize_entity("Northwind Freight's")[1] == "northwind_freight"

    def test_plural_metric_names_agree(self):
        assert (N.normalize_predicate("foreign exchange reserves")[1]
                == N.normalize_predicate("foreign exchange reserve")[1])

    def test_document_defined_acronyms_expand(self):
        acronyms = {"GDP": "gross domestic product"}
        _, key = N.normalize_predicate("real GDP growth", acronyms=acronyms)
        assert key == "real_gross_domestic_product_growth"

    def test_matching_tiers(self):
        assert N.predicate_match("ebitda_margin", "ebitda_margin")[0] == "exact"
        # A shortened form of the same name.
        assert N.predicate_match("reserve", "foreign_exchange_reserve")[0] == "head"
        # Each name carries its own distinguishing modifier: different measures.
        assert N.predicate_match("headline_inflation", "core_inflation")[0] is None
        assert N.predicate_match("total_income", "freight_tonnage")[0] is None
