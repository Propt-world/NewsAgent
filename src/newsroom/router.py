"""
Newsroom API, served by the scheduler app under /newscheduler/newsroom.

The routes are mounted only when NEWSROOM_ENABLED is true. When it is unset
or false, nothing here is registered and the scheduler behaves exactly as
before. All routes use the existing X-API-Key check.

X-Newsroom-User is recorded in the audit log as the acting user. It is sent
by the backoffice and is informational only (not verified) in this build.
"""
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Response, status

from src.newsroom import entities as svc
from src.newsroom.db import get_newsroom_db
from src.newsroom.models import (
    AliasCreate,
    AuditOut,
    EntityCreate,
    EntityListResponse,
    EntityOut,
    EntityUpdate,
    IgnoreCreate,
    IgnoreOut,
    NewsroomHealthResponse,
)
from src.utils.security import verify_api_key

router = APIRouter(
    prefix="/newsroom",
    tags=["Newsroom"],
    dependencies=[Depends(verify_api_key)],
)


def get_actor(x_newsroom_user: Optional[str] = Header(None)) -> str:
    actor = (x_newsroom_user or "").strip()[:200]
    return actor or "unknown"


async def _call(coro):
    try:
        return await coro
    except svc.NotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except svc.Conflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except svc.Invalid as e:
        raise HTTPException(status_code=422, detail=str(e))


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


# --- Entities ------------------------------------------------------------

@router.get("/entities", response_model=EntityListResponse, description="List and search entities with their aliases.")
async def list_entities(
    kind: Optional[Literal["organisation", "location"]] = None,
    entity_status: Optional[Literal["active", "staged", "ignored"]] = Query(None, alias="status"),
    entity_type: Optional[str] = Query(None, alias="type"),
    q: Optional[str] = Query(None, max_length=200, description="Matches the name or any alias"),
    limit: int = Query(100, ge=1, le=500),
    skip: int = Query(0, ge=0),
    db=Depends(get_newsroom_db),
):
    return await _call(svc.list_entities(db, kind, entity_status, entity_type, q, limit, skip))


@router.get("/entities/{entity_id}", response_model=EntityOut)
async def get_entity(entity_id: str, db=Depends(get_newsroom_db)):
    return await _call(svc.get_entity(db, entity_id))


@router.get("/entities/{entity_id}/audit", response_model=List[AuditOut], description="Most recent changes first.")
async def get_entity_audit(entity_id: str, limit: int = Query(50, ge=1, le=200), db=Depends(get_newsroom_db)):
    return await _call(svc.list_audit(db, entity_id, limit))


@router.post("/entities", response_model=EntityOut, status_code=status.HTTP_201_CREATED,
             description="Add an entity. The canonical name becomes an exact alias unless listed as ambiguous.")
async def create_entity(payload: EntityCreate, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(svc.create_entity(db, payload, actor))


@router.patch("/entities/{entity_id}", response_model=EntityOut,
              description="Edit name, type, status (on/off), parent, country or notes. Entities are switched off, never deleted.")
async def update_entity(entity_id: str, payload: EntityUpdate, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(svc.update_entity(db, entity_id, payload, actor))


@router.post("/entities/{entity_id}/aliases", response_model=EntityOut, status_code=status.HTTP_201_CREATED)
async def add_alias(entity_id: str, payload: AliasCreate, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(svc.add_alias(db, entity_id, payload, actor))


@router.delete("/entities/{entity_id}/aliases/{alias_id}", response_model=EntityOut)
async def delete_alias(entity_id: str, alias_id: str, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(svc.delete_alias(db, entity_id, alias_id, actor))


# --- Ignore list ---------------------------------------------------------

@router.get("/ignore", response_model=List[IgnoreOut], description="Strings that are never entities.")
async def list_ignore(db=Depends(get_newsroom_db)):
    return await _call(svc.list_ignore(db))


@router.post("/ignore", response_model=IgnoreOut, status_code=status.HTTP_201_CREATED)
async def add_ignore(payload: IgnoreCreate, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(svc.add_ignore(db, payload, actor))


@router.delete("/ignore/{ignore_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ignore(ignore_id: str, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    await _call(svc.delete_ignore(db, ignore_id, actor))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def mount_newsroom(app: FastAPI, enabled: bool) -> bool:
    """Registers the Newsroom routes on the app if enabled. Returns whether it did."""
    if not enabled:
        return False
    app.include_router(router)
    return True
