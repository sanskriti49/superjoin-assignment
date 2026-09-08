from app.routers.documents import router as documents_router
from app.routers.facts import router as facts_router
from app.routers.relationships import router as relationships_router
from app.routers.showcase import router as showcase_router
from app.routers.system import router as system_router

__all__ = [
    "documents_router",
    "facts_router",
    "relationships_router",
    "showcase_router",
    "system_router",
]
