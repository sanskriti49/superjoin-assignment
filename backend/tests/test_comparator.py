"""The rules that decide agreement, conflict and context."""

from app.pipeline.comparator import FactComparator


def fact(**overrides):
    base = {
        "id": "f1",
        "document_id": "docA",
        "subject_normalized": "acme_logistics",
        "subject": "Acme Logistics",
        "predicate": "operating_revenue",
        "predicate_label": "Operating revenue",
        "value_raw": "100 crore",
        "value_numeric": 1e9,
        "unit": "INR",
        "time_period": "FY24",
        "time_period_normalized": "2023-2024",
        "confidence": 0.9,
        "extraction_metadata": {},
    }
    base.update(overrides)
    return base


def pair(a_overrides=None, b_overrides=None):
    left = fact(**(a_overrides or {}))
    right = fact(id="f2", document_id="docB", **(b_overrides or {}))
    return FactComparator.compare_facts(left, right)


class TestVerdicts:
    def test_matching_values_corroborate(self):
        result = pair(b_overrides={"value_raw": "1,000 million", "value_numeric": 1e9})
        assert result["relationship_type"] == "CORROBORATED"
        assert result["reconciliation_factors"]["values_match"]

    def test_rounding_is_tolerated(self):
        result = pair(b_overrides={"value_raw": "100.5 crore", "value_numeric": 1.005e9})
        assert result["relationship_type"] == "CORROBORATED"

    def test_diverging_values_for_one_period_conflict(self):
        result = pair(b_overrides={"value_raw": "140 crore", "value_numeric": 1.4e9})
        assert result["relationship_type"] == "CONTRADICTED"
        assert "140 crore" in result["reasoning"]

    def test_a_different_reporting_basis_is_named_in_the_conflict(self):
        result = pair(
            {"qualifier": "First Advance Estimate"},
            {"value_numeric": 1.4e9, "value_raw": "140 crore",
             "qualifier": "Second Advance Estimate"},
        )
        assert result["relationship_type"] == "CONTRADICTED"
        assert "Second Advance Estimate" in result["reasoning"]

    def test_different_periods_explain_a_gap(self):
        result = pair(b_overrides={
            "value_raw": "140 crore", "value_numeric": 1.4e9,
            "time_period": "FY25", "time_period_normalized": "2024-2025"})
        assert result["relationship_type"] == "CONTEXTUALLY_DIFFERENT"
        assert result["reconciliation_factors"]["temporal_divergence"]

    def test_a_different_basis_explains_a_gap(self):
        result = pair(
            {"scope": "Standalone"},
            {"value_raw": "140 crore", "value_numeric": 1.4e9, "scope": "Consolidated"},
        )
        assert result["relationship_type"] == "CONTEXTUALLY_DIFFERENT"


class TestRefusals:
    def test_two_currencies_are_never_silently_compared(self):
        result = pair(b_overrides={"unit": "USD", "value_raw": "$1 billion"})
        assert result["relationship_type"] == "RELATED_BUT_NOT_COMPARABLE"
        assert "exchange rates" in result["reasoning"]

    def test_a_percentage_is_not_compared_with_money(self):
        result = pair(b_overrides={"unit": "%", "value_numeric": 1e9})
        assert result["relationship_type"] == "RELATED_BUT_NOT_COMPARABLE"

    def test_a_weak_extraction_yields_no_verdict(self):
        assert pair({"confidence": 0.4}, {"value_numeric": 1.4e9}) is None

    def test_no_conflict_is_claimed_when_a_period_was_inherited(self):
        # Neither sentence stated its own period, so "same period" is unproven.
        result = pair(
            {"extraction_metadata": {"period_inferred_from_document": True}},
            {"value_raw": "140 crore", "value_numeric": 1.4e9},
        )
        assert result is None or result["relationship_type"] != "CONTRADICTED"

    def test_facts_from_one_document_are_not_compared(self):
        left = fact()
        right = fact(id="f2", value_numeric=1.4e9)
        assert FactComparator.compare_facts(left, right) is None

    def test_different_subjects_are_not_compared(self):
        assert pair(b_overrides={"subject_normalized": "globex_shipping"}) is None

    def test_the_same_number_in_different_periods_is_not_corroboration(self):
        result = pair(b_overrides={"time_period": "FY25",
                                   "time_period_normalized": "2024-2025"})
        assert result is None


class TestPairing:
    def test_only_facts_sharing_metric_words_are_paired(self):
        facts = [
            fact(id="a", document_id="d1", predicate="operating_revenue"),
            fact(id="b", document_id="d2", predicate="operating_revenue"),
            fact(id="c", document_id="d2", predicate="headcount_total"),
        ]
        pairs = list(FactComparator.candidate_pairs(facts))
        assert ("a", "b") in {(left["id"], right["id"]) for left, right in pairs}
        assert all({left["id"], right["id"]} != {"a", "c"} for left, right in pairs)
