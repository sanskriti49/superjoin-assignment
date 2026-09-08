"""Test fixtures.

Every test runs against its own temporary database with the starter loader
switched off, so the suite never depends on, or writes to, the development
database.
"""

import os
import tempfile
from pathlib import Path

import pytest

TEMP_DIR = tempfile.mkdtemp(prefix="fkl-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DIR}/test.db"
os.environ["UPLOAD_DIR"] = f"{TEMP_DIR}/uploads"
os.environ["LOAD_STARTER_DATASET"] = "false"
os.environ["USE_LLM"] = "false"

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from tests.pdf_builder import build_pdf  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _clean_tables(db):
    """Each test starts from an empty knowledge layer."""
    from app.models import Document, DocumentPage, ExtractionIssue, Fact, FactRelationship
    for model in (FactRelationship, ExtractionIssue, Fact, DocumentPage, Document):
        db.query(model).delete()
    db.commit()
    yield


@pytest.fixture()
def make_pdf(tmp_path):
    """Write a small text PDF and return its path."""
    counter = {"n": 0}

    def _make(pages, name=None):
        counter["n"] += 1
        path = Path(tmp_path) / (name or f"doc{counter['n']}.pdf")
        path.write_bytes(build_pdf(pages))
        return path

    return _make


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client
