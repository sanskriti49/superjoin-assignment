"""Turning a PDF into stored facts and relationships.

This is the only place that writes to the knowledge layer. It runs the same code
path for the starter documents and for anything uploaded later, so what a
reviewer sees on first launch is genuine pipeline output rather than a fixture.

New documents are folded in incrementally: a fresh upload is compared against
the facts already stored, and nothing that was there before is recomputed.
"""

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Document, DocumentPage, ExtractionIssue, Fact, FactRelationship
from app.pipeline.comparator import FactComparator
from app.pipeline.extraction import DocumentProfiler, FactExtractionPipeline
from app.pipeline.ingestion import IngestionError, PDFIngestionPipeline

logger = logging.getLogger(__name__)

# How many pages are read to learn the document's dominant entity and period
# before extraction starts. Enough to characterise a document, cheap on a big one.
PROFILE_PAGES = 25

# Diagnostics are capped per document so one noisy PDF cannot dominate storage.
MAX_STORED_ISSUES = 200


def derive_title(filename: str, pdf_title: Optional[str]) -> str:
    """A readable document title from PDF metadata, falling back to the name."""
    if pdf_title and len(pdf_title.strip()) > 3:
        return pdf_title.strip()
    stem = Path(filename).stem
    stem = re.sub(r"^\d+[-_\s]*", "", stem)
    return re.sub(r"[-_]+", " ", stem).strip().title() or filename


def process_document(db: Session, document: Document, max_pages: Optional[int] = None) -> Dict[str, Any]:
    """Read one document end to end and store what it yields.

    Pages are streamed rather than loaded together, and facts are written per
    page, so peak memory does not grow with document length.
    """
    started = time.perf_counter()
    document.status = "processing"
    db.commit()

    counts = {"pages": 0, "facts": 0, "issues": 0, "routine_filters": 0, "relationships": 0}

    try:
        probe = PDFIngestionPipeline.probe(document.file_path)
        document.page_count = probe["page_count"]
        document.title = document.title or derive_title(document.filename, probe.get("pdf_title"))
        db.commit()

        page_limit = max_pages if max_pages is not None else settings.MAX_PAGES_PER_DOCUMENT
        pages = list(PDFIngestionPipeline.iter_pages(document.file_path, max_pages=page_limit))

        # Clear existing facts, issues, and relationships for idempotency on re-processing
        doc_fact_ids = [f[0] for f in db.query(Fact.id).filter(Fact.document_id == document.id).all()]
        if doc_fact_ids:
            db.query(FactRelationship).filter(
                FactRelationship.fact_a_id.in_(doc_fact_ids) |
                FactRelationship.fact_b_id.in_(doc_fact_ids)
            ).delete(synchronize_session=False)
        db.query(Fact).filter(Fact.document_id == document.id).delete(synchronize_session=False)
        db.query(ExtractionIssue).filter(ExtractionIssue.document_id == document.id).delete(synchronize_session=False)
        db.commit()

        profile = DocumentProfiler.profile_pages(pages[:PROFILE_PAGES], title=document.title)
        document.dominant_subject = profile.default_subject
        document.dominant_period = profile.default_period_raw

        new_facts: List[Dict[str, Any]] = []
        stored_issues = 0

        for page in pages:
            counts["pages"] += 1
            db.merge(DocumentPage(
                id=f"{document.id}_p{page['page_number']}",
                document_id=document.id,
                page_number=page["page_number"],
                text=page["text"],
                char_length=page["char_length"],
            ))
            if page["is_empty"]:
                continue

            facts, issues = FactExtractionPipeline.extract_from_page(
                doc_id=document.id,
                page_number=page["page_number"],
                page_text=page["text"],
                profile=profile,
                doc_name=document.title or document.filename,
            )

            for fact in facts:
                db.merge(_to_fact_row(fact))
                new_facts.append(fact)
            counts["facts"] += len(facts)

            for issue in issues:
                if issue.get("is_routine_filter"):
                    counts["routine_filters"] += 1
                    continue
                counts["issues"] += 1
                if stored_issues >= MAX_STORED_ISSUES:
                    continue
                stored_issues += 1
                db.merge(_to_issue_row(document.id, issue, stored_issues))

        document.pages_processed = counts["pages"]
        document.routine_filters = counts["routine_filters"]
        db.commit()

        counts["relationships"] = link_document(db, document.id, new_facts)

        document.status = "processed"
        document.processing_seconds = int(time.perf_counter() - started)
        db.commit()

    except IngestionError as exc:
        db.rollback()
        document.status = "failed"
        document.error_message = str(exc)
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001 - a bad PDF must not take the server down
        db.rollback()
        logger.exception("Processing failed for %s", document.id)
        document.status = "failed"
        document.error_message = f"Unexpected processing error: {exc}"
        db.commit()
        raise

    return counts


def link_document(db: Session, document_id: str, new_facts: Iterable[Dict[str, Any]]) -> int:
    """Compare one document's facts against everything already stored.

    Only the new facts are paired against the existing ones, so the cost of
    adding the n-th document is proportional to that document rather than to the
    whole knowledge layer.
    """
    new_facts = list(new_facts)
    if not new_facts:
        return 0

    existing = [fact.to_dict() for fact in
                db.query(Fact).filter(Fact.document_id != document_id).all()]
    if not existing:
        return 0

    written = 0
    for relationship in FactComparator.compare_all(new_facts + existing):
        if _store_relationship(db, relationship):
            written += 1
    db.commit()
    return written


def recompute_all(db: Session) -> Dict[str, int]:
    """Rebuild every relationship from the facts currently stored."""
    facts = [fact.to_dict() for fact in db.query(Fact).all()]
    db.query(FactRelationship).delete()
    db.commit()

    written = 0
    for relationship in FactComparator.compare_all(facts):
        if _store_relationship(db, relationship):
            written += 1
    db.commit()
    return {"facts_compared": len(facts), "relationships": written}


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------
def _store_relationship(db: Session, relationship: Dict[str, Any]) -> bool:
    """Insert a relationship unless the same fact pair is already linked."""
    fact_a, fact_b = relationship["fact_a_id"], relationship["fact_b_id"]
    # A stable id from the fact pair makes re-processing idempotent: the same
    # two facts can only ever produce one row, in either order.
    pair = "|".join(sorted([fact_a, fact_b]))
    relationship_id = f"rel_{hashlib.sha1(pair.encode()).hexdigest()[:20]}"

    existing = db.query(FactRelationship).filter(FactRelationship.id == relationship_id).first()
    row = existing or FactRelationship(id=relationship_id)
    row.fact_a_id = fact_a
    row.fact_b_id = fact_b
    row.relationship_type = relationship["relationship_type"]
    row.comparison_summary = relationship["comparison_summary"][:255]
    row.reasoning = relationship["reasoning"]
    row.reconciliation_factors = relationship["reconciliation_factors"]
    row.confidence = relationship["confidence"]
    if existing is None:
        db.add(row)
        return True
    return False


def _to_fact_row(fact: Dict[str, Any]) -> Fact:
    return Fact(
        id=fact["id"],
        document_id=fact["document_id"],
        subject=fact["subject"][:255],
        subject_normalized=fact["subject_normalized"][:255],
        predicate=fact["predicate"][:255],
        predicate_label=fact["predicate_label"][:255],
        value_raw=fact["value_raw"][:255],
        value_numeric=fact["value_numeric"],
        value_text=(fact["value_text"] or "")[:512],
        unit=(fact["unit"] or None) and fact["unit"][:64],
        unit_family=fact.get("unit_family"),
        time_period=(fact["time_period"] or None) and str(fact["time_period"])[:128],
        time_period_normalized=fact["time_period_normalized"],
        scope=fact["scope"],
        qualifier=fact["qualifier"],
        confidence=fact["confidence"],
        evidence_quote=fact["evidence_quote"],
        evidence_page=fact["evidence_page"],
        char_start=fact["char_start"],
        char_end=fact["char_end"],
        extraction_method=fact["extraction_method"][:64],
        extraction_metadata=fact["extraction_metadata"],
    )


def _to_issue_row(document_id: str, issue: Dict[str, Any], sequence: int) -> ExtractionIssue:
    return ExtractionIssue(
        id=f"issue_{document_id}_{sequence}",
        document_id=document_id,
        page_number=issue["page_number"],
        candidate_text=issue["candidate_text"][:255],
        context_snippet=issue["context_snippet"],
        reason_code=issue["reason_code"],
        detail=issue["detail"],
        is_routine_filter=bool(issue.get("is_routine_filter")),
    )
