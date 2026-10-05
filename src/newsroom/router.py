"""
Newsroom API, served by the scheduler app under /newscheduler/newsroom.

The routes are mounted only when NEWSROOM_ENABLED is true. When it is unset
or false, nothing here is registered and the scheduler behaves exactly as
before. All routes use the existing X-API-Key check.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, FastAPI

from src.newsroom.models import NewsroomHealthResponse
from src.utils.security import verify_api_key

router = APIRouter(
    prefix="/newsroom",
    tags=["Newsroom"],
    dependencies=[Depends(verify_api_key)],
)


@router.get(
    "/health",
    response_model=NewsroomHealthResponse,
    description="Confirms the Newsroom API is mounted. Performs no dependency checks.",
)
async def newsroom_health():
    return NewsroomHealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc),
    )


def mount_newsroom(app: FastAPI, enabled: bool) -> bool:
    """Registers the Newsroom routes on the app if enabled. Returns whether it did."""
    if not enabled:
        return False
    app.include_router(router)
    return True
