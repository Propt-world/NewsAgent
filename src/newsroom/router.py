"""
Newsroom API, served by the scheduler app under /newscheduler/newsroom.

The routes are mounted only when NEWSROOM_ENABLED is true. When it is unset
or false, nothing here is registered and the scheduler behaves exactly as
before. All routes use the existing X-API-Key check.

X-Newsroom-User is recorded in the audit log as the acting user. It is sent
by the backoffice and is informational only (not verified) in this build.
"""
from datetime import datetime, timezone
from typing import Annotated, List, Literal, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Query, Response, status
from pydantic import StringConstraints

from src.configs.settings import settings
from src.newsroom import compose as cmp
from src.newsroom import entities as svc
from src.newsroom import entity_csv
from src.newsroom import links as lnk
from src.newsroom import related as rel
from src.newsroom import status as health
from src.newsroom.db import get_item_sources, get_newsroom_db
from src.newsroom.models import (
    AliasCreate,
    AuditOut,
    ComposeRequest,
    ComposeRunList,
    ComposeRunOut,
    CsvExport,
    CsvImportRequest,
    CsvImportResult,
    CsvTemplate,
    CountResponse,
    EntityCreate,
    EntityListResponse,
    EntityOut,
    EntityUpdate,
    ExtractSignalRequest,
    FilteredItemsResponse,
    HistoryResponse,
    IgnoreCreate,
    IgnoreOut,
    ItemEntitiesResponse,
    ItemsChangedRequest,
    ItemsChangedResponse,
    ItemsSummaryRequest,
    ItemsSummaryResponse,
    LinkRequest,
    DeleteRequest,
    DeskSuggestion,
    DeskSuggestionRequest,
    DeleteResult,
    MergeRequest,
    MergeResult,
    NewNameDismissRequest,
    NewNamePage,
    NewNameLinkRequest,
    NewsroomHealthResponse,
    NewsroomStatusResponse,
    RelatedResponse,
    SignalSavedRequest,
    SourceCheck,
    SourceType,
    SuggestionField,
    SuggestionOut,
    SuggestRequest,
    SuggestResponse,
)
from src.utils.security import verify_api_key

# Ids in the address are short (uuids, ObjectIds, article keys); anything longer is refused.
PathId = Annotated[str, Path(min_length=1, max_length=100)]
StatusText = Annotated[str, StringConstraints(min_length=1, max_length=40)]

router = APIRouter(
    prefix="/newsroom",
    tags=["Newsroom"],
    dependencies=[Depends(verify_api_key)],
)


def get_actor(x_newsroom_user: Optional[str] = Header(None)) -> str:
    actor = (x_newsroom_user or "").strip()[:200]
    return actor or "unknown"


async def _rematched(db, entity: dict, actor: str) -> dict:
    """After an entity is added or changed: open "not in the list" suggestions with one of its names (on any item)
    become suggestions for it. Never links anything."""
    await lnk.rematch_names_of(db, entity["id"], actor)
    return entity


async def _call(coro):
    try:
        return await coro
    except svc.NotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except svc.Conflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except svc.Invalid as e:
        raise HTTPException(status_code=422, detail=str(e))
    except cmp.TooManyRequests as e:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))


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


@router.get(
    "/status",
    response_model=NewsroomStatusResponse,
    description="Problems that need attention (for the backoffice banner): database, compose queue and worker, "
                "waiting or failed compose jobs (last 24 h), the daily AI budget, and the real-estate filter rate. "
                "The scheduler logs the same problems as NEWSROOM_ALERT lines for CloudWatch.",
)
async def newsroom_status(db=Depends(get_newsroom_db), sources=Depends(get_item_sources)):
    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        return await health.check_status(db, sources.articles, client)
    finally:
        await client.aclose()


# --- Entities ------------------------------------------------------------

@router.get("/entities", response_model=EntityListResponse, description="List and search entities with their aliases.")
async def list_entities(
    kind: Optional[Literal["organisation", "location"]] = None,
    entity_status: Optional[Literal["active", "staged", "ignored", "merged"]] = Query(
        None, alias="status", description="Merged duplicates are listed only with status=merged"),
    entity_type: Optional[str] = Query(None, alias="type", max_length=64),
    q: Optional[str] = Query(None, max_length=200, description="Matches the name or any alias"),
    limit: int = Query(100, ge=1, le=500),
    skip: int = Query(0, ge=0),
    db=Depends(get_newsroom_db),
):
    return await _call(svc.list_entities(db, kind, entity_status, entity_type, q, limit, skip))


@router.get("/entities/export", response_model=CsvExport,
            description="One kind as CSV (the layout the import reads). Formula-like cells start with an apostrophe.")
async def export_entities(kind: Literal["organisation", "location"], db=Depends(get_newsroom_db)):
    label = "companies-people" if kind == "organisation" else "places"
    filename = f"newsroom-{label}-{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    return CsvExport(filename=filename, csv=await entity_csv.export_csv(db, kind))


@router.get("/entities/template", response_model=CsvTemplate,
            description="A sample file: the column names and example rows (rows named 'Example: ...' are never "
                        "imported), plus the accepted types.")
async def entities_template(kind: Literal["organisation", "location"], db=Depends(get_newsroom_db)):
    label = "companies-people" if kind == "organisation" else "places"
    return CsvTemplate(filename=f"newsroom-{label}-template.csv", csv=entity_csv.template_csv(kind),
                       columns=entity_csv.columns(kind), types=await entity_csv.allowed_types(db, kind))


@router.post("/entities/import", response_model=CsvImportResult,
             description="Add new entities from CSV. Names already in the list are skipped and never changed. "
                         "dry_run (the default) only checks; rows with a problem are never added.")
async def import_entities(payload: CsvImportRequest, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    result = await _call(entity_csv.import_csv(db, payload.kind, payload.csv_text, actor, payload.dry_run))
    if not payload.dry_run and result.get("added"):
        await lnk.rematch_unknown_names(db, actor=actor)  # names now in the list become suggestions for them
    return result


@router.get("/entities/{entity_id}", response_model=EntityOut)
async def get_entity(entity_id: PathId, db=Depends(get_newsroom_db)):
    return await _call(svc.get_entity(db, entity_id))


@router.get("/entities/{entity_id}/audit", response_model=List[AuditOut], description="Most recent changes first.")
async def get_entity_audit(entity_id: PathId, limit: int = Query(50, ge=1, le=200), db=Depends(get_newsroom_db)):
    return await _call(svc.list_audit(db, entity_id, limit))


@router.post("/entities", response_model=EntityOut, status_code=status.HTTP_201_CREATED,
             description="Add an entity. The canonical name becomes an exact alias unless listed as ambiguous.")
async def create_entity(payload: EntityCreate, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _rematched(db, await _call(svc.create_entity(db, payload, actor)), actor)


@router.patch("/entities/{entity_id}", response_model=EntityOut,
              description="Edit name, type, status (on/off), parent, country or notes. Entities are switched off, never deleted.")
async def update_entity(entity_id: PathId, payload: EntityUpdate, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _rematched(db, await _call(svc.update_entity(db, entity_id, payload, actor)), actor)


@router.post("/entities/{entity_id}/merge", response_model=MergeResult,
             description="Merge this duplicate into into_id: its aliases, links, suggestions and children move there. "
                         "Use dry_run to see the counts first. The duplicate stays as status 'merged'.")
async def merge_entity(entity_id: PathId, payload: MergeRequest, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    result = await _call(svc.merge_entities(db, entity_id, payload.into_id, actor, payload.dry_run))
    if not payload.dry_run:
        await lnk.rematch_names_of(db, payload.into_id, actor)
    return result


@router.post("/entities/{entity_id}/delete", response_model=DeleteResult,
             description='Delete an entity for good (its aliases and links; never articles or signals). '
                         'Needs confirm="delete"; use dry_run to see the counts first. Refused while entries sit '
                         'inside it or companies use it as their country.')
async def delete_entity(entity_id: PathId, payload: DeleteRequest, db=Depends(get_newsroom_db),
                        actor: str = Depends(get_actor)):
    return await _call(svc.delete_entity(db, entity_id, actor, payload.confirm, payload.dry_run))


@router.post("/entities/{entity_id}/aliases", response_model=EntityOut, status_code=status.HTTP_201_CREATED)
async def add_alias(entity_id: PathId, payload: AliasCreate, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _rematched(db, await _call(svc.add_alias(db, entity_id, payload, actor)), actor)


@router.delete("/entities/{entity_id}/aliases/{alias_id}", response_model=EntityOut)
async def delete_alias(entity_id: PathId, alias_id: PathId, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(svc.delete_alias(db, entity_id, alias_id, actor))


# --- Ignore list ---------------------------------------------------------

@router.get("/ignore", response_model=List[IgnoreOut], description="Strings that are never entities.")
async def list_ignore(db=Depends(get_newsroom_db)):
    return await _call(svc.list_ignore(db))


@router.post("/ignore", response_model=IgnoreOut, status_code=status.HTTP_201_CREATED)
async def add_ignore(payload: IgnoreCreate, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(svc.add_ignore(db, payload, actor))


@router.delete("/ignore/{ignore_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ignore(ignore_id: PathId, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
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
async def item_entities(source_type: SourceType, source_id: PathId, db=Depends(get_newsroom_db)):
    return await _call(lnk.item_entities(db, source_type, source_id))


@router.post("/items/{source_type}/{source_id}/links", response_model=ItemEntitiesResponse,
             description="Reviewer links an entity to the item (the only way a link is created).")
async def link_entity(source_type: SourceType, source_id: PathId, payload: LinkRequest, db=Depends(get_newsroom_db),
                      sources=Depends(get_item_sources), actor: str = Depends(get_actor)):
    return await _call(lnk.link(db, sources, source_type, source_id, payload.entity_id, actor,
                                suggestion_id=payload.suggestion_id, activate=payload.activate))


@router.post("/items/{source_type}/{source_id}/links/all", response_model=ItemEntitiesResponse,
             description="Links every open suggestion that points at a switched-on entity.")
async def link_all(source_type: SourceType, source_id: PathId, db=Depends(get_newsroom_db),
                   sources=Depends(get_item_sources), actor: str = Depends(get_actor)):
    return await _call(lnk.link_all(db, sources, source_type, source_id, actor))


@router.delete("/items/{source_type}/{source_id}/links/{entity_id}", response_model=ItemEntitiesResponse,
               description="Removes a link. It is remembered and never suggested again for this item.")
async def unlink_entity(source_type: SourceType, source_id: PathId, entity_id: PathId, db=Depends(get_newsroom_db),
                        actor: str = Depends(get_actor)):
    return await _call(lnk.unlink(db, source_type, source_id, entity_id, actor))


@router.get("/items/{source_type}/{source_id}/related", response_model=RelatedResponse,
            description="Other articles and signals sharing this item's confirmed companies (or, if it has none, "
                        "its specific places). Ranked by shared entities, then shared places, then recency. "
                        "An approved article with no links shows similar approved articles instead (no AI call).")
async def related_items(
    source_type: SourceType,
    source_id: PathId,
    location: Optional[str] = Query(None, max_length=100,
                                    description="Only items in this place (location entity id), e.g. Dubai"),
    days: Optional[int] = Query(None, ge=1, le=3650, description="Only items from the last N days"),
    limit: int = Query(20, ge=1, le=100),
    skip: int = Query(0, ge=0),
    db=Depends(get_newsroom_db),
    sources=Depends(get_item_sources),
):
    return await _call(rel.related_items(db, source_type, source_id, location, days, limit, skip, sources))


@router.get("/entities/{entity_id}/history", response_model=HistoryResponse,
            description="An entity's confirmed articles and signals, newest first, e.g. Emaar only in Dubai.")
async def entity_history(
    entity_id: PathId,
    location: Optional[str] = Query(None, max_length=100, description="Only items in this place (location entity id)"),
    before: Optional[datetime] = Query(None, description="Only items before this date"),
    after: Optional[datetime] = Query(None, description="Only items after this date"),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    db=Depends(get_newsroom_db),
):
    return await _call(rel.entity_history(db, entity_id, location, before, after, limit, skip))


@router.get("/items", response_model=FilteredItemsResponse,
            description="Articles and signals linked to a company and/or in a place (a place includes its "
                        "communities), newest first: the Newsroom list's company/place filter.")
async def filtered_items(
    entity: Optional[str] = Query(None, max_length=100, description="Only items linked to this entity"),
    location: Optional[str] = Query(None, max_length=100, description="Only items in this place (location entity id)"),
    source_type: Optional[SourceType] = Query(None, description="article or signal"),
    status: Optional[List[StatusText]] = Query(None, max_length=20,
                                               description="Item statuses, e.g. processed and draft for pending"),
    q: Optional[str] = Query(None, max_length=100, description="Plain text in the title"),
    limit: int = Query(20, ge=1, le=100),
    skip: int = Query(0, ge=0),
    db=Depends(get_newsroom_db),
):
    return await _call(rel.filtered_items(db, entity, location, source_type, status, q, limit, skip))


@router.post("/items/changed", response_model=ItemsChangedResponse,
             description="Another service changed these items (status or text). NewsAgent re-reads each one "
                         "and refreshes its links and the new-names queue. Only ids are sent.")
async def items_changed(payload: ItemsChangedRequest, db=Depends(get_newsroom_db), sources=Depends(get_item_sources)):
    updated = missing = 0
    for source_id in dict.fromkeys(payload.source_ids):
        if await _call(lnk.item_changed(db, sources, payload.source_type, source_id)):
            updated += 1
        else:
            missing += 1
    return {"updated": updated, "missing": missing}


@router.post("/suggestions/{suggestion_id}/dismiss", response_model=SuggestionOut)
async def dismiss_suggestion(suggestion_id: PathId, db=Depends(get_newsroom_db), actor: str = Depends(get_actor)):
    return await _call(lnk.dismiss(db, suggestion_id, actor))


@router.get("/new-names/page", response_model=NewNamePage,
            description="Names not in the list, from approved items, grouped and most frequent first, one page "
                        "at a time (titles for the first few items of each name). Optional: only companies and "
                        "people or only places (field), and part of the name (q).")
async def list_new_names(limit: int = Query(50, ge=1, le=200), skip: int = Query(0, ge=0, le=100_000),
                         field: Optional[SuggestionField] = None,
                         q: Optional[str] = Query(None, max_length=100, description="Part of the name, any case"),
                         db=Depends(get_newsroom_db), sources=Depends(get_item_sources)):
    return await _call(lnk.list_new_names(db, limit, skip, sources, field=field, search=q))


@router.post("/items/summary", response_model=ItemsSummaryResponse,
             description="Read-only: links and open suggestions per item, for badges in list screens.")
async def items_summary(payload: ItemsSummaryRequest, db=Depends(get_newsroom_db)):
    return {"items": await _call(lnk.items_summary(db, payload.source_type, payload.source_ids))}


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


# --- Compose (paid: the worker calls the AI model) ---------------------------------

async def get_redis():
    """FastAPI dependency: Redis for queueing compose jobs."""
    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@router.post("/compose", response_model=ComposeRunOut, status_code=status.HTTP_202_ACCEPTED,
             description="Queue a new Propt Newsroom article or signal from 2-5 approved or pending sources. "
                         "Checked against the hourly, concurrency and daily budget limits before anything is spent. "
                         "Sources that share no company or specific place need an article type (not update) "
                         "and cannot become one signal.")
async def compose(payload: ComposeRequest, db=Depends(get_newsroom_db), sources=Depends(get_item_sources),
                  redis=Depends(get_redis), actor: str = Depends(get_actor)):
    return await _call(cmp.create_run(
        db, sources, redis, kind="compose", output_type=payload.output_type,
        items=[s.model_dump() for s in payload.sources], actor=actor, exclude_entity_ids=payload.exclude_entity_ids,
        include_entity_ids=payload.include_entity_ids, compose_type=payload.compose_type, desk=payload.desk))


@router.post("/compose/desk-suggestion", response_model=DeskSuggestion,
             description="The specialist desk the sources' topics point to, as a hint for the compose window "
                         "(no AI call). Empty when no desk fits.")
async def compose_desk_suggestion(payload: DeskSuggestionRequest, sources=Depends(get_item_sources)):
    found = await cmp.desk_suggestion(sources, [s.model_dump() for s in payload.sources])
    return found or {}


@router.post("/compose/source-check", response_model=SourceCheck,
             description="For the compose window: whether the chosen sources can become a new article or signal "
                         "(at most one Propt Newsroom item; a new article needs an original article), a warning "
                         "when their topics share no desk, and earlier composes of exactly these sources. No AI call; the source rules are applied again when the "
                         "job is requested.")
async def compose_source_check(payload: DeskSuggestionRequest, sources=Depends(get_item_sources),
                               db=Depends(get_newsroom_db)):
    return await cmp.source_mix_problems(sources, [s.model_dump() for s in payload.sources], db)


@router.post("/extract-signal", response_model=ComposeRunOut, status_code=status.HTTP_202_ACCEPTED,
             description="Queue a check of one article: drafts up to 3 signals (one per angle the article states) "
                         "if there is one, otherwise says why not.")
async def extract_signal(payload: ExtractSignalRequest, db=Depends(get_newsroom_db), sources=Depends(get_item_sources),
                         redis=Depends(get_redis), actor: str = Depends(get_actor)):
    return await _call(cmp.create_run(
        db, sources, redis, kind="extract_signal", output_type="signal",
        items=[{"source_type": "article", "source_id": payload.article_id}], actor=actor,
        exclude_entity_ids=payload.exclude_entity_ids, include_entity_ids=payload.include_entity_ids))


@router.get("/compose", response_model=ComposeRunList, description="Recent compose runs and today's AI spend.")
async def list_compose_runs(limit: int = Query(20, ge=1, le=100), db=Depends(get_newsroom_db)):
    return await _call(cmp.list_runs(db, limit))


@router.get("/compose/{run_id}", response_model=ComposeRunOut)
async def get_compose_run(run_id: PathId, db=Depends(get_newsroom_db)):
    return await _call(cmp.get_run(db, run_id))


@router.post("/compose/{run_id}/saved", response_model=ComposeRunOut,
             description="The reviewer saved one drafted signal in social_scrapper; links the run's entities to it.")
async def compose_signal_saved(run_id: PathId, payload: SignalSavedRequest, db=Depends(get_newsroom_db),
                               sources=Depends(get_item_sources), actor: str = Depends(get_actor)):
    return await _call(cmp.mark_signal_saved(db, sources, run_id, payload.signal_id, actor, payload.draft))


@router.post("/compose/{run_id}/recover-saved", response_model=ComposeRunOut,
             description="After a save that stopped halfway (the signal exists in social_scrapper but the run was not "
                         "marked saved): finds the signal by its generated_from and finishes the save.")
async def compose_signal_recover(run_id: PathId, draft: int = Query(1, ge=1, le=3), db=Depends(get_newsroom_db),
                                 sources=Depends(get_item_sources), actor: str = Depends(get_actor)):
    return await _call(cmp.recover_saved_signal(db, sources, run_id, actor, draft))


def mount_newsroom(app: FastAPI, enabled: bool) -> bool:
    """Registers the Newsroom routes on the app if enabled. Returns whether it did."""
    if not enabled:
        return False
    app.include_router(router)
    return True
