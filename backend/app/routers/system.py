from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Document, ExtractionIssue, Fact, FactRelationship
from app.services.bootstrap import bootstrap_state

router = APIRouter(prefix="/system", tags=["System"])


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    relationship_counts = dict(
        db.query(FactRelationship.relationship_type, func.count(FactRelationship.id))
        .group_by(FactRelationship.relationship_type).all()
    )
    top_subjects = (db.query(Fact.subject, func.count(Fact.id).label("count"))
                    .group_by(Fact.subject)
                    .order_by(func.count(Fact.id).desc()).limit(6).all())
    top_metrics = (db.query(Fact.predicate_label, func.count(Fact.id).label("count"))
                   .group_by(Fact.predicate_label)
                   .order_by(func.count(Fact.id).desc()).limit(6).all())

    return {
        "documents": db.query(Document).count(),
        "pages": db.query(func.sum(Document.pages_processed)).scalar() or 0,
        "facts": db.query(Fact).count(),
        "distinct_metrics": db.query(func.count(func.distinct(Fact.predicate))).scalar() or 0,
        "relationships": db.query(FactRelationship).count(),
        "relationship_counts": relationship_counts,
        "mean_fact_confidence": round(
            float(db.query(func.avg(Fact.confidence)).scalar() or 0), 3),
        "rejected_candidates": db.query(ExtractionIssue).filter(
            ExtractionIssue.is_routine_filter.is_(False)).count(),
        "top_subjects": [{"subject": s, "count": c} for s, c in top_subjects],
        "top_metrics": [{"metric": m, "count": c} for m, c in top_metrics],
        "loading": bootstrap_state(),
    }
