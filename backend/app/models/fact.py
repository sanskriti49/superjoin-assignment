import datetime
from sqlalchemy import Column, String, Integer, Float, Text, JSON, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from app.database import Base

class Fact(Base):
    __tablename__ = "facts"

    id = Column(String(64), primary_key=True, index=True)
    document_id = Column(String(64), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Entity / Subject
    subject = Column(String(255), nullable=False, index=True)  # Raw entity name e.g. "Reserve Bank of India"
    subject_normalized = Column(String(255), nullable=False, index=True)  # Canonical entity e.g. "india" or "rbi"
    
    # Predicate / Attribute
    predicate = Column(String(255), nullable=False, index=True)  # Canonical predicate e.g. "foreign_exchange_reserves"
    predicate_label = Column(String(255), nullable=False)  # Display label e.g. "Foreign Exchange Reserves"
    
    # Value representation
    value_raw = Column(String(255), nullable=False)  # Raw string e.g. "US$ 668.3 billion"
    value_numeric = Column(Float, nullable=True)  # Normalized scalar e.g. 668300000000.0
    value_text = Column(String(512), nullable=True)  # For non-numeric or semantic facts
    
    # Contextual dimensions
    unit = Column(String(64), nullable=True)  # e.g. "USD", "%", "INR", "tonnes"
    # Coarse family ("currency", "ratio", "count"). Two facts in different
    # families are never compared numerically.
    unit_family = Column(String(32), nullable=True, index=True)
    time_period = Column(String(128), nullable=True)  # Raw e.g. "as at end-March 2025" or "FY2024-25"
    time_period_normalized = Column(String(128), nullable=True, index=True)  # e.g. "2024-2025", "2025-03-31"
    scope = Column(String(128), nullable=True)  # e.g. "Central Government", "General Government", "All India"
    qualifier = Column(String(255), nullable=True)  # e.g. "First Advance Estimate", "Provisional", "Consolidated"
    
    # Grounding & Verification
    confidence = Column(Float, nullable=False, default=1.0)
    evidence_quote = Column(Text, nullable=False)  # Verbatim sentence or snippet from the PDF
    evidence_page = Column(Integer, nullable=False)  # 1-indexed PDF page number
    char_start = Column(Integer, nullable=True)
    char_end = Column(Integer, nullable=True)
    
    # Method & Metadata
    extraction_method = Column(String(64), default="pattern")  # which strategy read this fact
    extraction_metadata = Column(JSON, default=dict)  # Stores pattern match, reasoning, prompt version, etc.
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    document = relationship("Document", back_populates="facts")

    def to_dict(self):
        return {
            "id": self.id,
            "document_id": self.document_id,
            "document_name": self.document.filename if self.document else None,
            "subject": self.subject,
            "subject_normalized": self.subject_normalized,
            "predicate": self.predicate,
            "predicate_label": self.predicate_label,
            "value_raw": self.value_raw,
            "value_numeric": self.value_numeric,
            "value_text": self.value_text,
            "unit": self.unit,
            "unit_family": self.unit_family,
            "time_period": self.time_period,
            "time_period_normalized": self.time_period_normalized,
            "scope": self.scope,
            "qualifier": self.qualifier,
            "confidence": self.confidence,
            "evidence": {
                "document": self.document.filename if self.document else self.document_id,
                "page": self.evidence_page,
                "quote": self.evidence_quote,
                "char_start": self.char_start,
                "char_end": self.char_end
            },
            "extraction_method": self.extraction_method,
            "extraction_metadata": self.extraction_metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }
