from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    # The API and the background loader touch the same connection pool.
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)

if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        # SQLite ignores foreign keys unless asked, which would leave orphaned
        # facts behind a deleted document. WAL lets reads continue during writes.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    import app.models  # noqa: F401  - registers the mappers before create_all
    Base.metadata.create_all(bind=engine)
