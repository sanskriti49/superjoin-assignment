"""Cross-document comparison and reconciliation.

Two facts are only worth comparing when they describe the same thing: the same
subject, the same metric, and quantities in compatible units. Once that holds,
the outcome is decided by whether the numbers agree and, when they do not,
whether some stated difference in context explains the gap.

The decision is deliberately rule-based rather than delegated to a model. Every
classification carries the exact factors that produced it, so a reader can
disagree with the conclusion by pointing at the factor they think is wrong.
"""

import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.pipeline.normalizer import FactNormalizer

CORROBORATED = "CORROBORATED"
CONTRADICTED = "CONTRADICTED"
CONTEXTUALLY_DIFFERENT = "CONTEXTUALLY_DIFFERENT"
RELATED_NOT_COMPARABLE = "RELATED_BUT_NOT_COMPARABLE"


class FactComparator:
    """Compares fact pairs and explains the verdict."""

    # Published figures are rounded, so a gap this small is agreement.
    TOLERANCE = 0.015
    # Below this, an extraction is not trustworthy enough to reason about.
    MIN_CONFIDENCE_FOR_VERDICT = 0.65
    # How much of the shorter metric name the longer one must contain before an
    # inexact name match may carry a verdict.
    MIN_OVERLAP_FOR_VERDICT = 0.8

    # ------------------------------------------------------------------
    # Pair generation
    # ------------------------------------------------------------------
    # A predicate word shared by more than this fraction of all facts is too
    # common to narrow anything down, so it is not used for blocking.
    COMMON_TOKEN_FRACTION = 0.15

    @classmethod
    def candidate_pairs(
        cls, facts: Iterable[Dict[str, Any]]
    ) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Yield only the fact pairs worth comparing.

        Comparing every fact with every other is quadratic and almost entirely
        wasted, since two facts about different metrics can never relate. Facts
        are instead indexed by the words in their predicate, and only facts
        sharing a word are ever paired. Very common words are skipped for
        indexing because they do not narrow anything down, and the head noun is
        always indexed so that a weak head-only match is still found.

        This is what keeps a new document cheap to fold in: its facts only need
        to be looked up against the words they actually use.
        """
        facts = list(facts)
        if not facts:
            return

        frequency: Dict[str, int] = {}
        for fact in facts:
            for token in set((fact.get("predicate") or "").split("_")):
                if token:
                    frequency[token] = frequency.get(token, 0) + 1

        common_cutoff = max(2, int(len(facts) * cls.COMMON_TOKEN_FRACTION))
        index: Dict[str, List[int]] = {}
        for position, fact in enumerate(facts):
            tokens = [t for t in (fact.get("predicate") or "").split("_") if t]
            if not tokens:
                continue
            keys = {token for token in tokens if frequency[token] <= common_cutoff}
            keys.add(tokens[-1])
            for key in keys:
                index.setdefault(key, []).append(position)

        seen: set = set()
        for bucket in index.values():
            for offset, left_position in enumerate(bucket):
                for right_position in bucket[offset + 1:]:
                    pair = (left_position, right_position)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    left, right = facts[left_position], facts[right_position]
                    if left.get("document_id") != right.get("document_id"):
                        yield left, right

    @classmethod
    def compare_all(cls, facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results = []
        for left, right in cls.candidate_pairs(facts):
            relationship = cls.compare_facts(left, right)
            if relationship:
                results.append(relationship)
        return results

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------
    @classmethod
    def compare_facts(
        cls, fact_a: Dict[str, Any], fact_b: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Classify one pair of facts, or return None when they are unrelated."""
        if fact_a.get("document_id") == fact_b.get("document_id"):
            return None

        subject_a = (fact_a.get("subject_normalized") or "").lower()
        subject_b = (fact_b.get("subject_normalized") or "").lower()
        if not FactNormalizer.entities_compatible(subject_a, subject_b):
            return None

        predicate_a = (fact_a.get("predicate") or "").lower()
        predicate_b = (fact_b.get("predicate") or "").lower()
        predicate_tier, predicate_overlap = FactNormalizer.predicate_match(predicate_a, predicate_b)

        if predicate_tier is None:
            return None

        factors = cls._collect_factors(fact_a, fact_b)
        factors["predicate_match_basis"] = predicate_tier
        factors["predicate_word_overlap"] = predicate_overlap
        pair_confidence = round(min(cls._conf(fact_a), cls._conf(fact_b)), 3)

        # A verdict on a pair the extractor is unsure about, or whose metric
        # names only roughly agree, is more likely to mislead than to inform.
        if pair_confidence < cls.MIN_CONFIDENCE_FOR_VERDICT:
            return None
        if predicate_tier == "phrase" and predicate_overlap < cls.MIN_OVERLAP_FOR_VERDICT:
            return None

        if predicate_tier == "head":
            # Only the head noun agreed, so the two names may still describe
            # different things. Say so in the score rather than hiding it.
            pair_confidence = round(pair_confidence * 0.8, 3)

        # Guard 1: units that cannot be placed on the same scale.
        if not factors["units_comparable"]:
            return cls._build(
                RELATED_NOT_COMPARABLE, fact_a, fact_b,
                summary=f"Incompatible units: {fact_a.get('predicate_label')}",
                reasoning=(
                    f"Both documents report {fact_a.get('predicate_label')} for "
                    f"{fact_a.get('subject')}, but in units that cannot be compared without an "
                    f"external conversion: {fact_a.get('unit') or 'unitless'} against "
                    f"{fact_b.get('unit') or 'unitless'}. The system does not hold exchange "
                    f"rates or deflators, so it declines to call this agreement or conflict."
                ),
                factors=factors,
                confidence=pair_confidence,
            )

        value_a, value_b = fact_a.get("value_numeric"), fact_b.get("value_numeric")
        if value_a is None or value_b is None:
            return cls._compare_text(fact_a, fact_b, factors, pair_confidence)

        if factors["values_match"] and factors["temporal_divergence"]:
            # The same number in two different periods is a coincidence, or a
            # figure that simply did not move. Neither is corroboration.
            return None

        if factors["values_match"]:
            return cls._build(
                CORROBORATED, fact_a, fact_b,
                summary=f"Corroborated: {fact_a.get('predicate_label')}",
                reasoning=cls._corroboration_reasoning(fact_a, fact_b, factors),
                factors=factors,
                confidence=pair_confidence,
            )

        # The numbers disagree. Anything in the stated context that accounts for
        # the gap turns an apparent conflict into a contextual difference.
        if factors["temporal_divergence"]:
            return cls._build(
                CONTEXTUALLY_DIFFERENT, fact_a, fact_b,
                summary=f"Different periods: {fact_a.get('predicate_label')}",
                reasoning=(
                    f"{fact_a.get('subject')} {fact_a.get('predicate_label')} is reported as "
                    f"{fact_a.get('value_raw')} and {fact_b.get('value_raw')}, a difference of "
                    f"{factors['relative_difference'] * 100:.1f} per cent. The two figures "
                    f"cover different periods: '{factors['fact_a_period']}' against "
                    f"'{factors['fact_b_period']}'. They measure the same quantity at "
                    f"different moments, so both can be correct."
                ),
                factors=factors,
                confidence=pair_confidence,
            )

        if factors["scope_divergence"]:
            return cls._build(
                CONTEXTUALLY_DIFFERENT, fact_a, fact_b,
                summary=f"Different scope: {fact_a.get('predicate_label')}",
                reasoning=(
                    f"{fact_a.get('value_raw')} and {fact_b.get('value_raw')} differ by "
                    f"{factors['relative_difference'] * 100:.1f} per cent, but they are reported "
                    f"on different bases: '{factors['fact_a_scope']}' against "
                    f"'{factors['fact_b_scope']}'. A narrower scope covers less than a wider "
                    f"one, so the figures are not in conflict."
                ),
                factors=factors,
                confidence=pair_confidence,
            )

        # Two numbers can only be in conflict if they describe the same period.
        # When either period had to be inferred from the document rather than
        # read from the sentence, that has not been established, and the honest
        # answer is that the pair cannot be judged.
        if not factors["period_established"]:
            if predicate_tier != "exact" or pair_confidence < 0.7:
                return None
            return cls._build(
                RELATED_NOT_COMPARABLE, fact_a, fact_b,
                summary=f"Period not established: {fact_a.get('predicate_label')}",
                reasoning=(
                    f"{fact_a.get('value_raw')} and {fact_b.get('value_raw')} differ by "
                    f"{factors['relative_difference'] * 100:.1f} per cent, but at least one of "
                    f"them does not state its period in the sentence it was taken from "
                    f"('{factors['fact_a_period']}' against '{factors['fact_b_period']}'). "
                    f"Without knowing that both cover the same span, a conflict cannot be "
                    f"claimed, so the pair is left unjudged."
                ),
                factors=factors,
                confidence=pair_confidence,
            )

        if predicate_tier == "head":
            # Two names sharing only a head noun, with numbers that disagree, is
            # the most likely shape of a false pairing. Recording it would bury
            # the real findings, so nothing is emitted.
            return None

        if factors["qualifier_divergence"]:
            return cls._build(
                CONTRADICTED, fact_a, fact_b,
                summary=f"Revision conflict: {fact_a.get('predicate_label')}",
                reasoning=(
                    f"For the same period '{factors['fact_a_period']}', "
                    f"{fact_a.get('subject')} {fact_a.get('predicate_label')} is given as "
                    f"{fact_a.get('value_raw')} ({factors['fact_a_qualifier']}) and "
                    f"{fact_b.get('value_raw')} ({factors['fact_b_qualifier']}). The figures "
                    f"conflict, and the stated basis differs, which points at a data revision "
                    f"between releases rather than an error in either document. A reader taking "
                    f"either number at face value would be misled about the other."
                ),
                factors=factors,
                confidence=pair_confidence,
            )

        stated_basis = factors["fact_a_qualifier"] if factors["fact_a_qualifier"] != "unstated" \
            else factors["fact_b_qualifier"]
        if factors["qualifier_one_sided"]:
            closing = (
                f"One of the two names the basis it reports on ({stated_basis}) and the other "
                f"does not. A figure published on a stated basis and one published without one "
                f"are the usual shape of a revision between releases, so this reads as a likely "
                f"contradiction rather than a certain one."
            )
        else:
            closing = "Nothing in the surrounding text accounts for the difference."

        return cls._build(
            CONTRADICTED, fact_a, fact_b,
            summary=f"Direct conflict: {fact_a.get('predicate_label')}",
            reasoning=(
                f"Both documents report {fact_a.get('predicate_label')} for "
                f"{fact_a.get('subject')} over '{factors['fact_a_period']}' with no stated "
                f"difference in scope, yet the values disagree: "
                f"{fact_a.get('value_raw')} against {fact_b.get('value_raw')}, a gap of "
                f"{factors['relative_difference'] * 100:.1f} per cent. {closing}"
            ),
            factors=factors,
            confidence=pair_confidence,
        )

    # ------------------------------------------------------------------
    # Factors
    # ------------------------------------------------------------------
    @classmethod
    def _collect_factors(cls, fact_a: Dict[str, Any], fact_b: Dict[str, Any]) -> Dict[str, Any]:
        period_a = fact_a.get("time_period_normalized")
        period_b = fact_b.get("time_period_normalized")
        scope_a, scope_b = fact_a.get("scope"), fact_b.get("scope")
        qualifier_a, qualifier_b = fact_a.get("qualifier"), fact_b.get("qualifier")
        unit_a, unit_b = fact_a.get("unit"), fact_b.get("unit")
        value_a, value_b = fact_a.get("value_numeric"), fact_b.get("value_numeric")

        relative_difference = 0.0
        if value_a is not None and value_b is not None:
            scale = max(abs(value_a), abs(value_b))
            relative_difference = abs(value_a - value_b) / scale if scale else 0.0

        meta_a = fact_a.get("extraction_metadata") or {}
        meta_b = fact_b.get("extraction_metadata") or {}
        period_stated = (
            bool(period_a) and bool(period_b)
            and not meta_a.get("period_inferred_from_document")
            and not meta_b.get("period_inferred_from_document")
        )

        return {
            "subject_match": True,
            "predicate_match": True,
            "period_established": period_stated and period_a == period_b,
            "period_stated_in_both": period_stated,
            "units_comparable": FactNormalizer.units_comparable(unit_a, unit_b),
            "unit_a": unit_a,
            "unit_b": unit_b,
            "values_match": relative_difference <= cls.TOLERANCE,
            "relative_difference": round(relative_difference, 4),
            "temporal_match": bool(period_stated and period_a == period_b),
            "temporal_divergence": bool(period_stated and period_a != period_b),
            "scope_divergence": bool(scope_a and scope_b and scope_a != scope_b),
            "qualifier_divergence": bool(qualifier_a and qualifier_b and qualifier_a != qualifier_b),
            # One document names the basis it reports on and the other does not.
            # That is not proof of a revision, but it is the most common reason
            # two otherwise identical claims carry different numbers.
            "qualifier_one_sided": bool(qualifier_a) != bool(qualifier_b),
            "fact_a_period": fact_a.get("time_period") or "unstated",
            "fact_b_period": fact_b.get("time_period") or "unstated",
            "fact_a_scope": scope_a or "unstated",
            "fact_b_scope": scope_b or "unstated",
            "fact_a_qualifier": qualifier_a or "unstated",
            "fact_b_qualifier": qualifier_b or "unstated",
        }

    @staticmethod
    def _conf(fact: Dict[str, Any]) -> float:
        try:
            return float(fact.get("confidence", 1.0))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _corroboration_reasoning(
        cls, fact_a: Dict[str, Any], fact_b: Dict[str, Any], factors: Dict[str, Any]
    ) -> str:
        raw_a, raw_b = fact_a.get("value_raw"), fact_b.get("value_raw")
        normalized = FactNormalizer.describe_value(fact_a.get("value_numeric"), fact_a.get("unit"))

        if raw_a != raw_b:
            wording = (
                f"The two documents write the figure differently, as '{raw_a}' and '{raw_b}', "
                f"but both normalize to {normalized}."
            )
        else:
            wording = f"Both documents state the same figure, {raw_a}."

        period = (f"for the same period '{factors['fact_a_period']}'"
                  if factors["temporal_match"] else "with no conflicting period stated")
        return (
            f"{wording} They agree on {fact_a.get('predicate_label')} for "
            f"{fact_a.get('subject')} {period}, within the {cls.TOLERANCE * 100:.1f} per cent "
            f"tolerance allowed for rounding. Two independently published sources reporting the "
            f"same value is corroboration."
        )

    @classmethod
    def _compare_text(
        cls,
        fact_a: Dict[str, Any],
        fact_b: Dict[str, Any],
        factors: Dict[str, Any],
        confidence: float,
    ) -> Dict[str, Any]:
        text_a = (fact_a.get("value_text") or fact_a.get("value_raw") or "").strip().lower()
        text_b = (fact_b.get("value_text") or fact_b.get("value_raw") or "").strip().lower()

        if text_a and text_a == text_b:
            return cls._build(
                CORROBORATED, fact_a, fact_b,
                summary=f"Corroborated: {fact_a.get('predicate_label')}",
                reasoning=(
                    f"Neither figure could be reduced to a number, but both documents assert "
                    f"the same value '{fact_a.get('value_raw')}' for "
                    f"{fact_a.get('predicate_label')}."
                ),
                factors=factors,
                confidence=confidence,
            )

        return cls._build(
            RELATED_NOT_COMPARABLE, fact_a, fact_b,
            summary=f"Not numerically comparable: {fact_a.get('predicate_label')}",
            reasoning=(
                f"At least one of '{fact_a.get('value_raw')}' and '{fact_b.get('value_raw')}' "
                f"could not be reduced to a number, so the two cannot be checked against each "
                f"other."
            ),
            factors=factors,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _build(
        relationship_type: str,
        fact_a: Dict[str, Any],
        fact_b: Dict[str, Any],
        summary: str,
        reasoning: str,
        factors: Dict[str, Any],
        confidence: float,
    ) -> Dict[str, Any]:
        return {
            "id": f"rel_{uuid.uuid4().hex[:16]}",
            "fact_a_id": fact_a["id"],
            "fact_b_id": fact_b["id"],
            "relationship_type": relationship_type,
            "comparison_summary": summary,
            "reasoning": reasoning,
            "reconciliation_factors": factors,
            "confidence": confidence,
        }
