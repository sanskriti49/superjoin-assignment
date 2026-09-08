from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DocumentPage, Fact, FactRelationship

router = APIRouter(prefix="/facts", tags=["Facts"])


@router.get("")
def list_facts(
    document_id: Optional[str] = Query(None),
    subject: Optional[str] = Query(None),
    predicate: Optional[str] = Query(None),
    unit_family: Optional[str] = Query(None),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    search: Optional[str] = Query(None, description="Matches subject, metric, value or quote"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Fact)
    if document_id:
        query = query.filter(Fact.document_id == document_id)
    if subject:
        query = query.filter(Fact.subject_normalized.ilike(f"%{subject}%"))
    if predicate:
        query = query.filter(Fact.predicate.ilike(f"%{predicate}%"))
    if unit_family:
        query = query.filter(Fact.unit_family == unit_family)
    if min_confidence > 0:
        query = query.filter(Fact.confidence >= min_confidence)
    if search:
        term = f"%{search}%"
        query = query.filter(or_(
            Fact.subject.ilike(term),
            Fact.predicate_label.ilike(term),
            Fact.value_raw.ilike(term),
            Fact.evidence_quote.ilike(term),
        ))

    total = query.count()
    rows = (query.order_by(Fact.confidence.desc(), Fact.id.asc())
            .offset(offset).limit(limit).all())
    return {"total": total, "offset": offset, "limit": limit,
            "items": [fact.to_dict() for fact in rows]}


@router.get("/schema")
def discovered_schema(db: Session = Depends(get_db)):
    """Every metric the system has minted from the documents it has read.

    There is no predefined list of metrics: each row here appeared because some
    document used those words, which is what makes the schema grow on its own as
    new kinds of documents arrive.
    """
    rows = (db.query(
                Fact.predicate,
                func.min(Fact.predicate_label).label("label"),
                func.count(Fact.id).label("fact_count"),
                func.count(func.distinct(Fact.document_id)).label("document_count"),
                func.min(Fact.unit_family).label("unit_family"),
                func.avg(Fact.confidence).label("mean_confidence"))
            .group_by(Fact.predicate)
            .order_by(func.count(func.distinct(Fact.document_id)).desc(),
                      func.count(Fact.id).desc())
            .all())
    return {
        "total_predicates": len(rows),
        "shared_across_documents": sum(1 for row in rows if row.document_count > 1),
        "predicates": [
            {
                "predicate": row.predicate,
                "label": row.label,
                "fact_count": row.fact_count,
                "document_count": row.document_count,
                "unit_family": row.unit_family,
                "mean_confidence": round(float(row.mean_confidence or 0), 3),
            }
            for row in rows
        ],
    }


@router.get("/{fact_id}")
def get_fact(fact_id: str, db: Session = Depends(get_db)):
    fact = db.query(Fact).filter(Fact.id == fact_id).first()
    if not fact:
        raise HTTPException(status_code=404, detail="Fact not found")

    relationships = (db.query(FactRelationship)
                     .filter(or_(FactRelationship.fact_a_id == fact_id,
                                 FactRelationship.fact_b_id == fact_id)).all())

    page = (db.query(DocumentPage)
            .filter(DocumentPage.document_id == fact.document_id,
                    DocumentPage.page_number == fact.evidence_page).first())

    payload = fact.to_dict()
    payload["relationships"] = [row.to_dict() for row in relationships]
    # Re-check the quote against the stored page rather than trusting the flag
    # written at extraction time.
    payload["grounding"] = {
        "page_available": page is not None,
        "quote_found_in_page": bool(page and fact.evidence_quote in page.text),
        "offsets_match": bool(
            page and fact.char_start is not None
            and page.text[fact.char_start:fact.char_end] == fact.evidence_quote
        ),
    }
    return payload
