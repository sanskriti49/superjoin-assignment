import uuid
from typing import List, Optional

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Query, UploadFile)
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import Document, DocumentPage, ExtractionIssue, Fact, FactRelationship
from app.pipeline.ingestion import IngestionError, PDFIngestionPipeline
from app.services.knowledge_layer import derive_title, process_document

router = APIRouter(prefix="/documents", tags=["Documents"])


def _process_in_background(document_id: str) -> None:
    """Run the pipeline on its own session, detached from the request."""
    db = SessionLocal()
    try:
        document = db.query(Document).filter(Document.id == document_id).first()
        if document:
            process_document(db, document)
    except Exception:  # noqa: BLE001 - the failure is already recorded on the row
        pass
    finally:
        db.close()


@router.post("/upload")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    dataset_category: Optional[str] = Form("upload"),
    wait: bool = Query(True, description="Process before responding. Set false for large files."),
    db: Session = Depends(get_db),
):
    """Accept a PDF and run it through the pipeline.

    Processing is synchronous by default so a reviewer sees results immediately,
    and can be pushed to the background for a document large enough that the
    request would otherwise time out.
    """
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    document_id = f"doc_{uuid.uuid4().hex[:12]}"
    path = settings.UPLOAD_DIR / f"{document_id}.pdf"
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024

    try:
        written = 0
        with open(path, "wb") as target:
            while chunk := await file.read(1 << 20):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.",
                    )
                target.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not store upload: {exc}") from exc

    file_hash = PDFIngestionPipeline.compute_sha256(str(path))
    existing = db.query(Document).filter(Document.file_hash == file_hash).first()
    if existing:
        # The same bytes are already in the knowledge layer, so reprocessing
        # would only duplicate work and create self-referential relationships.
        path.unlink(missing_ok=True)
        payload = existing.to_dict()
        payload["duplicate_of"] = existing.id
        payload["message"] = "This document is already loaded; its existing record is returned."
        return payload

    document = Document(
        id=document_id,
        filename=file.filename,
        title=derive_title(file.filename, None),
        file_path=str(path),
        file_hash=file_hash,
        file_size_bytes=written,
        dataset_category=dataset_category or "upload",
        status="pending",
    )
    db.add(document)
    db.commit()

    if not wait:
        background_tasks.add_task(_process_in_background, document_id)
        return document.to_dict(fact_count=0)

    try:
        counts = process_document(db, document)
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}") from exc

    payload = document.to_dict(fact_count=counts["facts"])
    payload["extraction"] = counts
    return payload


@router.get("")
def list_documents(db: Session = Depends(get_db)) -> List[dict]:
    counts = dict(db.query(Fact.document_id, func.count(Fact.id))
                  .group_by(Fact.document_id).all())
    from sqlalchemy import distinct
    rel_rows = db.query(Fact.document_id, func.count(distinct(FactRelationship.id)))\
                 .join(FactRelationship, (Fact.id == FactRelationship.fact_a_id) | (Fact.id == FactRelationship.fact_b_id))\
                 .group_by(Fact.document_id).all()
    rel_counts = dict(rel_rows)
    documents = db.query(Document).order_by(Document.created_at.desc()).all()
    res = []
    for document in documents:
        d = document.to_dict(fact_count=counts.get(document.id, 0))
        d["relationship_count"] = rel_counts.get(document.id, 0)
        res.append(d)
    return res


@router.get("/{document_id}")
def get_document(document_id: str, db: Session = Depends(get_db)):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    facts = (db.query(Fact).filter(Fact.document_id == document_id)
             .order_by(Fact.evidence_page.asc(), Fact.confidence.desc()).all())
    issues = (db.query(ExtractionIssue)
              .filter(ExtractionIssue.document_id == document_id,
                      ExtractionIssue.is_routine_filter.is_(False))
              .limit(50).all())

    by_page: dict = {}
    for fact in facts:
        by_page.setdefault(fact.evidence_page, []).append(fact.to_dict())

    return {
        "document": document.to_dict(fact_count=len(facts)),
        "total_facts": len(facts),
        "facts": [fact.to_dict() for fact in facts],
        "facts_by_page": by_page,
        "issues": [issue.to_dict() for issue in issues],
    }


@router.get("/{document_id}/pages/{page_number}")
def get_page_text(document_id: str, page_number: int, db: Session = Depends(get_db)):
    """The stored text of one page, so an evidence quote can be checked by eye."""
    page = (db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id,
                    DocumentPage.page_number == page_number).first())
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return {
        "document_id": document_id,
        "page_number": page.page_number,
        "char_length": page.char_length,
        "text": page.text,
    }


@router.delete("/{document_id}")
def delete_document(document_id: str, db: Session = Depends(get_db)):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    fact_ids = [row[0] for row in db.query(Fact.id).filter(Fact.document_id == document_id).all()]
    if fact_ids:
        (db.query(FactRelationship)
         .filter(FactRelationship.fact_a_id.in_(fact_ids)
                 | FactRelationship.fact_b_id.in_(fact_ids))
         .delete(synchronize_session=False))

    db.delete(document)
    db.commit()
    return {"deleted": document_id, "facts_removed": len(fact_ids)}
