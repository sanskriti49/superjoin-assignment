from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FactRelationship
from app.services.knowledge_layer import recompute_all

router = APIRouter(prefix="/relationships", tags=["Relationships"])


@router.get("")
def list_relationships(
    relationship_type: Optional[str] = Query(None),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(FactRelationship)
    if relationship_type:
        query = query.filter(FactRelationship.relationship_type == relationship_type.upper())
    if min_confidence > 0:
        query = query.filter(FactRelationship.confidence >= min_confidence)

    total = query.count()
    rows = (query.order_by(FactRelationship.confidence.desc())
            .offset(offset).limit(limit).all())

    counts = dict(db.query(FactRelationship.relationship_type,
                           func.count(FactRelationship.id))
                  .group_by(FactRelationship.relationship_type).all())

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "counts": counts,
        "items": [row.to_dict() for row in rows],
    }


@router.get("/{relationship_id}")
def get_relationship(relationship_id: str, db: Session = Depends(get_db)):
    row = db.query(FactRelationship).filter(FactRelationship.id == relationship_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Relationship not found")
    return row.to_dict()


@router.post("/recompute")
def recompute(db: Session = Depends(get_db)):
    """Rebuild all relationships. Normal ingestion is incremental; this is a reset."""
    return recompute_all(db)
