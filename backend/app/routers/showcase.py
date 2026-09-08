from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.showcase import build_cases

router = APIRouter(prefix="/showcase", tags=["Showcase"])


@router.get("/cases")
def list_cases(db: Session = Depends(get_db)):
    """The four required cases, selected live from the stored relationships."""
    return {"cases": build_cases(db)}
