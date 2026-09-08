import os
from pathlib import Path
from typing import List, Optional

try:
    from pydantic_settings import BaseSettings
except ImportError:  # pydantic v1 fallback
    from pydantic import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseSettings):
    PROJECT_NAME: str = "Fact Knowledge Layer"
    VERSION: str = "2.0.0"
    API_V1_STR: str = "/api"

    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/fact_knowledge.db")
    UPLOAD_DIR: Path = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
    STARTER_DIR: Path = Path(os.getenv("STARTER_DIR", str(BASE_DIR.parent / "starter-datasets")))

    # Load the starter PDFs through the normal pipeline on an empty database.
    LOAD_STARTER_DATASET: bool = _flag("LOAD_STARTER_DATASET", "true")
    # 0 means read every page. A cap bounds the work a single upload can cause.
    MAX_PAGES_PER_DOCUMENT: Optional[int] = int(os.getenv("MAX_PAGES_PER_DOCUMENT", "0")) or None
    MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "100"))

    # Optional model-assisted extraction. Off unless a key and the flag are set.
    USE_LLM: bool = _flag("USE_LLM")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    CORS_ORIGINS: List[str] = os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
