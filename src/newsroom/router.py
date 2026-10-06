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
from src.newsroom import links as lnk
from src.newsroom.db import get_item_sources, get_newsroom_db
from src.newsroom.models import (
    AliasCreate,
    AuditOut,
    CountResponse,
    EntityCreate,
    EntityListResponse,
    EntityOut,
    EntityUpdate,
    IgnoreCreate,
    IgnoreOut,
    ItemEntitiesResponse,
    LinkRequest,
    NewNameDismissRequest,
    NewNameGroup,
    NewNameLinkRequest,
    NewsroomHealthResponse,
    SourceType,
    SuggestionOut,
    SuggestRequest,
    SuggestResponse,
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


# --- Suggestions and links ---------------------------------------------------

@router.post("/suggest", response_model=SuggestResponse,
             description="Turns names found on an article or signal into suggestions (exact matches only). "
                         "Writes nothing to the graph. Safe to call again.")
async def suggest(payload: SuggestRequest, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(lnk.suggest(db, payload.source_type, payload.source_id, payload.organisations,
                                   payload.locations, actor if actor != "unknown" else "system"))


@router.get("/items/{source_type}/{source_id}/entities", response_model=ItemEntitiesResponse,
            description="Linked entities and open suggestions for one article or signal.")
async def item_entities(source_type: SourceType, source_id: str, db=Depends(get_newsroom_db)):
    return await _call(lnk.item_entities(db, source_type, source_id))


@router.post("/items/{source_type}/{source_id}/links", response_model=ItemEntitiesResponse,
             description="Reviewer links an entity to the item (the only way a link is created).")
async def link_entity(source_type: SourceType, source_id: str, payload: LinkRequest, db=Depends(get_newsroom_db),
                      sources=Depends(get_item_sources), actor: str = Depends(get_actor)):
    return await _call(lnk.link(db, sources, source_type, source_id, payload.entity_id, actor,
                                suggestion_id=payload.suggestion_id, activate=payload.activate))


@router.post("/items/{source_type}/{source_id}/links/all", response_model=ItemEntitiesResponse,
             description="Links every open suggestion that points at a switched-on entity.")
async def link_all(source_type: SourceType, source_id: str, db=Depends(get_newsroom_db),
                   sources=Depends(get_item_sources), actor: str = Depends(get_actor)):
    return await _call(lnk.link_all(db, sources, source_type, source_id, actor))


@router.delete("/items/{source_type}/{source_id}/links/{entity_id}", response_model=ItemEntitiesResponse,
               description="Removes a link. It is remembered and never suggested again for this item.")
async def unlink_entity(source_type: SourceType, source_id: str, entity_id: str, db=Depends(get_newsroom_db),
                        actor: str = Depends(get_actor)):
    return await _call(lnk.unlink(db, source_type, source_id, entity_id, actor))


@router.post("/suggestions/{suggestion_id}/dismiss", response_model=SuggestionOut)
async def dismiss_suggestion(suggestion_id: str, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(lnk.dismiss(db, suggestion_id, actor))


@router.get("/new-names", response_model=List[NewNameGroup],
            description="Names not in the list, from approved items, grouped and most frequent first.")
async def list_new_names(limit: int = Query(100, ge=1, le=500), db=Depends(get_newsroom_db)):
    return await _call(lnk.list_new_names(db, limit))


@router.post("/new-names/link", response_model=CountResponse,
             description="Links every queued item carrying this name to an entity (for example one just created).")
async def link_new_name(payload: NewNameLinkRequest, db=Depends(get_newsroom_db), sources=Depends(get_item_sources),
                        actor: str = Depends(get_actor)):
    count = await _call(lnk.link_new_name(db, sources, payload.field, payload.name_norm, payload.entity_id, actor,
                                          activate=payload.activate))
    return {"count": count}


@router.post("/new-names/dismiss", response_model=CountResponse)
async def dismiss_new_name(payload: NewNameDismissRequest, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return {"count": await _call(lnk.dismiss_new_name(db, payload.field, payload.name_norm, actor))}


def mount_newsroom(app: FastAPI, enabled: bool) -> bool:
    """Registers the Newsroom routes on the app if enabled. Returns whether it did."""
    if not enabled:
        return False
    app.include_router(router)
    return True
