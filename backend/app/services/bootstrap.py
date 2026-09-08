"""First-run loading of the starter dataset.

The starter PDFs are put through exactly the same pipeline as an upload. There
is no fixture and no pre-written answer: if the extractor gets something wrong,
it is wrong on screen too. Loading happens on a background thread so the API is
usable immediately, and it is skipped entirely when the database already holds
documents.
"""

import logging
import threading
import uuid
from pathlib import Path
from typing import List

from app.config import settings
from app.database import SessionLocal
from app.models import Document
from app.pipeline.ingestion import PDFIngestionPipeline
from app.services.knowledge_layer import derive_title, process_document

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state = {"running": False, "loaded": 0, "total": 0, "current": None, "finished": False}


def bootstrap_state() -> dict:
    with _lock:
        return dict(_state)


def find_starter_pdfs() -> List[Path]:
    root = Path(settings.STARTER_DIR)
    if not root.exists():
        return []
    return sorted(root.rglob("*.pdf"))


def load_starter_dataset_async() -> None:
    """Kick off starter loading in the background if there is anything to load."""
    if not settings.LOAD_STARTER_DATASET:
        return

    db = SessionLocal()
    try:
        if db.query(Document).count() > 0:
            return
    finally:
        db.close()

    pdfs = find_starter_pdfs()
    if not pdfs:
        logger.info("No starter dataset found at %s", settings.STARTER_DIR)
        return

    with _lock:
        if _state["running"]:
            return
        _state.update({"running": True, "loaded": 0, "total": len(pdfs),
                       "current": None, "finished": False})

    thread = threading.Thread(target=_load, args=(pdfs,), name="starter-loader", daemon=True)
    thread.start()


def _load(pdfs: List[Path]) -> None:
    db = SessionLocal()
    try:
        for pdf in pdfs:
            with _lock:
                _state["current"] = pdf.name
            try:
                _ingest_one(db, pdf)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
                logger.warning("Starter document %s failed: %s", pdf.name, exc)
            with _lock:
                _state["loaded"] += 1
    finally:
        db.close()
        with _lock:
            _state.update({"running": False, "current": None, "finished": True})
        logger.info("Starter dataset loaded: %s documents", _state["loaded"])


def _ingest_one(db, pdf: Path) -> None:
    file_hash = PDFIngestionPipeline.compute_sha256(str(pdf))
    if db.query(Document).filter(Document.file_hash == file_hash).first():
        return

    document = Document(
        id=f"doc_{uuid.uuid4().hex[:12]}",
        filename=pdf.name,
        title=derive_title(pdf.name, None),
        file_path=str(pdf.resolve()),
        file_hash=file_hash,
        file_size_bytes=pdf.stat().st_size,
        dataset_category=pdf.parent.name,
        status="pending",
    )
    db.add(document)
    db.commit()
    process_document(db, document)
