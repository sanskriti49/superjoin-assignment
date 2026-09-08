import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class ExtractionIssue(Base):
    """A quantity the extractor saw and chose not to keep, with the reason.

    Recording refusals is what makes the extractor's failure modes inspectable.
    Routine filters (page numbers, years in running text) are counted rather
    than stored one by one; everything else is stored in full so it can be read
    back through the API.
    """

    __tablename__ = "extraction_issues"

    id = Column(String(80), primary_key=True, index=True)
    document_id = Column(String(64), ForeignKey("documents.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    page_number = Column(Integer, nullable=False)
    candidate_text = Column(String(255), nullable=False, default="")
    context_snippet = Column(Text, nullable=False, default="")
    reason_code = Column(String(64), nullable=False, index=True)
    detail = Column(Text, nullable=False, default="")
    is_routine_filter = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    document = relationship("Document", back_populates="issues")

    def to_dict(self):
        return {
            "id": self.id,
            "document_id": self.document_id,
            "page_number": self.page_number,
            "candidate_text": self.candidate_text,
            "context_snippet": self.context_snippet,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }
