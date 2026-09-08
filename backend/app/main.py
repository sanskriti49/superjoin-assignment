import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import (documents_router, facts_router, relationships_router,
                         showcase_router, system_router)
from app.services.bootstrap import load_starter_dataset_async

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Reads the starter PDFs through the ordinary pipeline on a background
    # thread, so the API answers immediately while the first load runs.
    load_starter_dataset_async()
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=(
        "Extracts measured facts from PDFs, keeps each one tied to the exact text it "
        "came from, and compares facts across documents to say where they agree, "
        "where they conflict, and where an apparent conflict is explained by context."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (documents_router, facts_router, relationships_router,
               showcase_router, system_router):
    app.include_router(router, prefix=settings.API_V1_STR)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": settings.PROJECT_NAME, "version": settings.VERSION}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
