import datetime
from sqlalchemy import Column, String, Float, Text, JSON, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from app.database import Base

class FactRelationship(Base):
    __tablename__ = "fact_relationships"

    id = Column(String(64), primary_key=True, index=True)
    fact_a_id = Column(String(64), ForeignKey("facts.id", ondelete="CASCADE"), nullable=False, index=True)
    fact_b_id = Column(String(64), ForeignKey("facts.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Relationship type: CORROBORATED, CONTRADICTED, CONTEXTUALLY_DIFFERENT, RELATED_BUT_NOT_COMPARABLE
    relationship_type = Column(String(64), nullable=False, index=True)
    
    # Summary title
    comparison_summary = Column(String(255), nullable=False)
    
    # Transparent reasoning explaining WHY the system reached this conclusion
    reasoning = Column(Text, nullable=False)
    
    # Detailed breakdown of comparison dimensions:
    # {
    #   "entity_match": true,
    #   "predicate_match": true,
    #   "value_match": true/false,
    #   "temporal_match": true/false,
    #   "scope_match": true/false,
    #   "differences": ["temporal", "qualifier"]
    # }
    reconciliation_factors = Column(JSON, default=dict)
    
    confidence = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    fact_a = relationship("Fact", foreign_keys=[fact_a_id])
    fact_b = relationship("Fact", foreign_keys=[fact_b_id])

    def to_dict(self):
        return {
            "id": self.id,
            "fact_a_id": self.fact_a_id,
            "fact_b_id": self.fact_b_id,
            "fact_a": self.fact_a.to_dict() if self.fact_a else None,
            "fact_b": self.fact_b.to_dict() if self.fact_b else None,
            "relationship_type": self.relationship_type,
            "comparison_summary": self.comparison_summary,
            "reasoning": self.reasoning,
            "reconciliation_factors": self.reconciliation_factors,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }
