import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class Document(Base):
    """One ingested PDF."""

    __tablename__ = "documents"

    id = Column(String(64), primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    title = Column(String(255), nullable=True)
    file_path = Column(String(512), nullable=False)
    file_hash = Column(String(64), unique=True, index=True, nullable=False)
    file_size_bytes = Column(Integer, nullable=False, default=0)
    page_count = Column(Integer, nullable=False, default=0)
    pages_processed = Column(Integer, nullable=False, default=0)
    # Numbers seen and discarded as page numbers, list markers or bare dates.
    routine_filters = Column(Integer, nullable=False, default=0)
    dataset_category = Column(String(64), nullable=True, default="user_upload")

    # The entity and reporting period the document is mostly about, learned from
    # the document itself and used only where a sentence does not say its own.
    dominant_subject = Column(String(255), nullable=True)
    dominant_period = Column(String(128), nullable=True)

    status = Column(String(32), nullable=False, default="pending")  # pending|processing|processed|failed
    error_message = Column(Text, nullable=True)
    processing_seconds = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    facts = relationship("Fact", back_populates="document", cascade="all, delete-orphan")
    pages = relationship("DocumentPage", back_populates="document", cascade="all, delete-orphan")
    issues = relationship("ExtractionIssue", back_populates="document", cascade="all, delete-orphan")

    def to_dict(self, fact_count=None):
        return {
            "id": self.id,
            "filename": self.filename,
            "title": self.title,
            "page_count": self.page_count,
            "pages_processed": self.pages_processed,
            "routine_filters": self.routine_filters,
            "file_size_bytes": self.file_size_bytes,
            "dataset_category": self.dataset_category,
            "dominant_subject": self.dominant_subject,
            "dominant_period": self.dominant_period,
            "status": self.status,
            "error_message": self.error_message,
            "processing_seconds": self.processing_seconds,
            "fact_count": fact_count if fact_count is not None else len(self.facts),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DocumentPage(Base):
    """The canonical text of one page, kept so evidence can be re-verified.

    Storing the exact text an evidence offset points into is what turns the
    grounding claim into something a reader can check rather than trust.
    """

    __tablename__ = "document_pages"

    id = Column(String(80), primary_key=True, index=True)
    document_id = Column(String(64), ForeignKey("documents.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    page_number = Column(Integer, nullable=False, index=True)
    text = Column(Text, nullable=False, default="")
    char_length = Column(Integer, nullable=False, default=0)

    document = relationship("Document", back_populates="pages")
