"""The four required cases, selected from whatever is actually in the database.

Nothing here is written by hand. Each case is a query over the relationships the
comparator produced, so the examples change as documents are added or removed,
and an empty result is reported honestly rather than papered over.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Document, ExtractionIssue, Fact, FactRelationship
from app.pipeline.comparator import (
    CONTEXTUALLY_DIFFERENT,
    CONTRADICTED,
    CORROBORATED,
)
from app.pipeline.normalizer import FactNormalizer

CASE_DEFINITIONS = [
    {
        "case_number": "CASE_1",
        "title": "A fact corroborated across documents",
        "question": "Do two independent documents state the same thing, even when they word it differently?",
        "relationship_type": CORROBORATED,
    },
    {
        "case_number": "CASE_2",
        "title": "A genuine or likely contradiction",
        "question": "Do two documents state figures for the same thing and the same period that cannot both be right?",
        "relationship_type": CONTRADICTED,
    },
    {
        "case_number": "CASE_3",
        "title": "An apparent contradiction explained by context",
        "question": "Do figures that look like a conflict turn out to describe different periods or a different basis?",
        "relationship_type": CONTEXTUALLY_DIFFERENT,
    },
    {
        "case_number": "CASE_4",
        "title": "Extraction and reasoning failures",
        "question": "Where does the system get things wrong, and what does it do about it?",
        "relationship_type": None,
    },
]


def build_cases(db: Session) -> List[Dict[str, Any]]:
    return [
        _comparison_case(db, definition) if definition["relationship_type"]
        else _failure_case(db, definition)
        for definition in CASE_DEFINITIONS
    ]


# ---------------------------------------------------------------------------
# Cases 1 to 3
# ---------------------------------------------------------------------------
def _comparison_case(db: Session, definition: Dict[str, Any]) -> Dict[str, Any]:
    relationship = _pick_relationship(db, definition["relationship_type"])
    payload: Dict[str, Any] = {
        "case_number": definition["case_number"],
        "title": definition["title"],
        "question": definition["question"],
        "relationship_type": definition["relationship_type"],
        "available": relationship is not None,
    }

    if relationship is None:
        payload["explanation"] = (
            "No pair of this kind is present in the documents currently loaded. Upload "
            "documents that cover a common metric and this case will fill itself in."
        )
        return payload

    payload.update({
        "relationship_id": relationship.id,
        "summary": relationship.comparison_summary,
        "reasoning": relationship.reasoning,
        "confidence": relationship.confidence,
        "comparison": _readable_factors(relationship.reconciliation_factors or {}),
        "factors": relationship.reconciliation_factors or {},
        "fact_a": _fact_panel(relationship.fact_a),
        "fact_b": _fact_panel(relationship.fact_b),
        "alternatives": _count_of_type(db, definition["relationship_type"]),
    })
    return payload


def _pick_relationship(db: Session, relationship_type: str) -> Optional[FactRelationship]:
    """Choose the clearest example of a relationship type.

    For corroboration the interesting case is two documents that write the same
    figure differently, so those are preferred over identical strings.
    """
    query = (db.query(FactRelationship)
             .filter(FactRelationship.relationship_type == relationship_type)
             .order_by(FactRelationship.confidence.desc()))

    candidates = query.limit(60).all()
    if not candidates:
        return None

    def score(relationship: FactRelationship) -> tuple:
        """Rank by how much a reader learns from the example, then by confidence.

        A pair whose reasoning names something concrete (a stated reporting
        basis, a period difference) teaches more than one that only says two
        numbers differ, so those are shown first.
        """
        fact_a, fact_b = relationship.fact_a, relationship.fact_b
        if not fact_a or not fact_b:
            return (-1, 0)

        factors = relationship.reconciliation_factors or {}
        points = 0
        points += int(fact_a.value_raw != fact_b.value_raw)
        points += int(bool(fact_a.time_period and fact_b.time_period))
        points += int(factors.get("predicate_match_basis") == "exact")
        points += 2 * int(bool(factors.get("qualifier_divergence")
                               or factors.get("qualifier_one_sided")))
        points += 2 * int(bool(factors.get("scope_divergence")))
        points += int(fact_a.document_id != fact_b.document_id)
        return (points, relationship.confidence)

    return max(candidates, key=score)


def _fact_panel(fact: Optional[Fact]) -> Optional[Dict[str, Any]]:
    if fact is None:
        return None
    metadata = fact.extraction_metadata or {}
    return {
        "id": fact.id,
        "document_id": fact.document_id,
        "document": fact.document.title if fact.document else fact.document_id,
        "filename": fact.document.filename if fact.document else None,
        "page": fact.evidence_page,
        "subject": fact.subject,
        "predicate": fact.predicate_label,
        "value_raw": fact.value_raw,
        "value_normalized": FactNormalizer.describe_value(fact.value_numeric, fact.unit),
        "unit": fact.unit,
        "time_period": fact.time_period or "not stated",
        "time_period_normalized": fact.time_period_normalized,
        "scope": fact.scope,
        "qualifier": fact.qualifier,
        "confidence": fact.confidence,
        "quote": fact.evidence_quote,
        "char_start": fact.char_start,
        "char_end": fact.char_end,
        "period_inferred": bool(metadata.get("period_inferred_from_document")),
        "subject_inferred": bool(metadata.get("subject_inferred_from_document")),
    }


def _readable_factors(factors: Dict[str, Any]) -> List[Dict[str, str]]:
    """Turn the comparator's decision factors into rows a reader can scan."""
    if not factors:
        return []

    def yes_no(value: Any) -> str:
        return "yes" if value else "no"

    rows = [
        {"label": "Metric names", "value": {
            "exact": "identical",
            "phrase": f"overlap {factors.get('predicate_word_overlap', 0):.0%}",
            "head": "same head noun only",
        }.get(factors.get("predicate_match_basis"), "matched")},
        {"label": "Units", "value": (
            f"{factors.get('unit_a') or 'none'} and {factors.get('unit_b') or 'none'}"
            + ("" if factors.get("units_comparable", True) else ", not comparable"))},
        {"label": "Values agree", "value": yes_no(factors.get("values_match"))},
        {"label": "Difference", "value": f"{factors.get('relative_difference', 0) * 100:.2f}%"},
        {"label": "Period A", "value": str(factors.get("fact_a_period", "unstated"))},
        {"label": "Period B", "value": str(factors.get("fact_b_period", "unstated"))},
        {"label": "Same period", "value": yes_no(factors.get("temporal_match"))},
    ]
    if factors.get("scope_divergence"):
        rows.append({"label": "Basis", "value":
                     f"{factors.get('fact_a_scope')} against {factors.get('fact_b_scope')}"})
    if factors.get("qualifier_divergence"):
        rows.append({"label": "Reporting basis", "value":
                     f"{factors.get('fact_a_qualifier')} against {factors.get('fact_b_qualifier')}"})
    return rows


def _count_of_type(db: Session, relationship_type: str) -> int:
    return (db.query(FactRelationship)
            .filter(FactRelationship.relationship_type == relationship_type)
            .count())


# ---------------------------------------------------------------------------
# Case 4
# ---------------------------------------------------------------------------
def _failure_case(db: Session, definition: Dict[str, Any]) -> Dict[str, Any]:
    """Report the extractor's own refusals and the weakest facts it kept.

    The four categories below are read out of the database, not written down in
    advance, so this section stays accurate for documents nobody has seen.
    """
    grouped = (db.query(ExtractionIssue.reason_code,
                        func.count(ExtractionIssue.id).label("count"))
               .filter(ExtractionIssue.is_routine_filter.is_(False))
               .group_by(ExtractionIssue.reason_code)
               .order_by(func.count(ExtractionIssue.id).desc())
               .all())

    categories = []
    for reason_code, count in grouped:
        sample = (db.query(ExtractionIssue)
                  .filter(ExtractionIssue.reason_code == reason_code)
                  .first())
        categories.append({
            "reason_code": reason_code,
            "count": count,
            "explanation": sample.detail if sample else "",
            "example": {
                "candidate": sample.candidate_text,
                "context": sample.context_snippet,
                "page": sample.page_number,
                "document": _document_name(db, sample.document_id),
            } if sample else None,
        })

    routine = db.query(func.sum(Document.routine_filters)).scalar() or 0

    weakest = (db.query(Fact)
               .filter(Fact.confidence < 0.6)
               .order_by(Fact.confidence.asc())
               .limit(5).all())

    inferred_periods = (db.query(func.count(Fact.id))
                        .filter(Fact.time_period_normalized.is_(None)).scalar() or 0)

    return {
        "case_number": definition["case_number"],
        "title": definition["title"],
        "question": definition["question"],
        "relationship_type": None,
        "available": True,
        "categories": categories,
        "routine_filter_count": routine,
        "facts_without_period": inferred_periods,
        "weakest_facts": [
            {
                "id": fact.id,
                "predicate": fact.predicate_label,
                "value_raw": fact.value_raw,
                "confidence": fact.confidence,
                "page": fact.evidence_page,
                "document": fact.document.title if fact.document else fact.document_id,
                "quote": fact.evidence_quote[:400],
                "signals": (fact.extraction_metadata or {}).get("confidence_signals", {}),
            }
            for fact in weakest
        ],
        "known_limits": _known_limits(),
    }


def _known_limits() -> List[Dict[str, str]]:
    """Failure modes found while building this, stated plainly.

    These describe the design, not any particular document, so they are written
    here rather than derived. Everything above this point is derived.
    """
    return [
        {
            "name": "A change is read as a level",
            "detail": (
                "In 'EBITDA increased by Rs. 578 Cr to Rs. 127 Cr', both numbers are captured "
                "with the same metric name, but the first is a movement and the second is a "
                "level. The extractor has no notion of delta against level, so a movement can "
                "be compared against a level and look like a conflict."
            ),
            "handling": (
                "Partly contained by the rule that a contradiction requires both figures to "
                "state their own period, which movements usually do not. Reading the verb "
                "('increased by' against 'stood at') would fix it properly."
            ),
        },
        {
            "name": "A period is inherited from the document",
            "detail": (
                "When a sentence names no period, the document's dominant period is used and "
                "the fact is flagged. That guess is often right and sometimes wrong."
            ),
            "handling": (
                "Such facts are never allowed to produce a contradiction, and the flag is "
                "visible on every fact so the inference can be seen rather than assumed."
            ),
        },
        {
            "name": "A subject is inherited from the document",
            "detail": (
                "A sentence that names no entity is attributed to whatever the document is "
                "mostly about. In a report that compares several countries, a figure about "
                "one of them can be attributed to the document's main subject."
            ),
            "handling": (
                "An entity named in the sentence always wins, and the flag is carried on the "
                "fact. Resolving pronouns and comparison clauses would be the real fix."
            ),
        },
        {
            "name": "Tables lose their row and column headers",
            "detail": (
                "PDF text extraction flattens a table into lines. Where a value's meaning "
                "lives in a column header several lines above, the neighbouring-line rule "
                "picks up the wrong caption or none at all."
            ),
            "handling": (
                "Such candidates are usually rejected outright, which is why 'no metric label "
                "in context' is a common rejection. Positional extraction would recover them."
            ),
        },
        {
            "name": "Two currencies are never converted",
            "detail": (
                "A figure in rupees and a figure in dollars are not compared, because the "
                "system holds no exchange rates and a stale rate would fabricate a verdict."
            ),
            "handling": (
                "The pair is recorded as not comparable, with the two units named, rather "
                "than guessed at."
            ),
        },
    ]


def _document_name(db: Session, document_id: str) -> str:
    document = db.query(Document).filter(Document.id == document_id).first()
    return document.title if document and document.title else document_id
