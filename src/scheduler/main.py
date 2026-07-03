import asyncio
import os
import traceback
import uuid
import boto3
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager
import logging
import sys

from fastapi import (
    FastAPI,
    HTTPException,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    status,
    File,
    UploadFile,
    Query,
    Response
)
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pymongo.errors import DuplicateKeyError
import httpx
import redis.asyncio as aioredis

from src.configs.settings import settings
from src.scheduler.db import (
    close_async_mongo_client,
    close_legacy_mongo_client,
    get_async_scheduler_collections,
    ping_async_database,
)
from src.scheduler.models import SourceConfig, ProcessedArticle, PaginatedArticleResponse
from src.scheduler.link_discovery import fetch_listing_page, extract_valid_urls
from src.utils.email_utils import close_async_email_client, send_error_email_async
from src.utils.governance import close_async_governance_clients
from src.utils.security import verify_api_key, verify_webhook_secret
from src.models.Responses import (
    GenericResponse,
    SchedulerDatabaseHealthResponse,
    SchedulerHealthResponse,
    SchedulerStandardHealthResponse,
)
from src.utils.image_compressor import compress_image
from src.utils.mongo_store import get_mongo_store
from src.utils.chunker import Chunker
from src.utils.loader import normalize_document
import tempfile
import shutil

# LOGGING SETUP
logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler")

# HEALTH CHECK SETUP
HEALTH_CHECK_TIMEOUT_SECONDS = float(os.getenv("HEALTH_CHECK_TIMEOUT_SECONDS", "3.0"))
_dependency_status_cache: Dict[str, Dict[str, Any]] = {
    "database": {"status": "unknown", "checked_at": None},
    "redis": {"status": "unknown", "checked_at": None},
    "main_api": {"status": "unknown", "checked_at": None},
    "browserless": {"status": "unknown", "checked_at": None},
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _remember_dependency_status(name: str, status_value: str) -> str:
    _dependency_status_cache[name] = {
        "status": status_value,
        "checked_at": _utc_now(),
    }
    return status_value


def _last_known_dependency_status(name: str) -> str:
    status_value = _dependency_status_cache.get(name, {}).get("status", "unknown")
    if status_value == "unknown":
        return "unknown"
    return f"last_known_{status_value}"


def _scheduler_status() -> str:
    return "running" if scheduler.running else "stopped"


async def check_database_dependency() -> str:
    try:
        await asyncio.wait_for(
            ping_async_database(),
            timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        return _remember_dependency_status("database", "connected")
    except asyncio.TimeoutError:
        logger.warning("[HEALTH] MongoDB ping timed out.")
        return _remember_dependency_status("database", "timeout")
    except Exception as e:
        logger.warning(f"[HEALTH] MongoDB ping failed: {e}")
        return _remember_dependency_status("database", "disconnected")


async def check_redis_dependency() -> str:
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await asyncio.wait_for(
            redis_client.ping(),
            timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        return _remember_dependency_status("redis", "connected")
    except asyncio.TimeoutError:
        logger.warning("[HEALTH] Redis ping timed out.")
        return _remember_dependency_status("redis", "timeout")
    except Exception as e:
        logger.warning(f"[HEALTH] Redis ping failed: {e}")
        return _remember_dependency_status("redis", "disconnected")
    finally:
        try:
            await redis_client.aclose()
        except Exception:
            pass


async def check_main_api_dependency() -> str:
    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as http:
            resp = await http.get(
                f"{settings.MAIN_API_URL}/health",
                params={"check_external": "false"},
            )

        if resp.status_code == 200:
            return _remember_dependency_status("main_api", "reachable")
        return _remember_dependency_status("main_api", f"degraded_{resp.status_code}")
    except httpx.TimeoutException:
        logger.warning("[HEALTH] Main API health check timed out.")
        return _remember_dependency_status("main_api", "timeout")
    except Exception as e:
        logger.warning(f"[HEALTH] Main API health check failed: {e}")
        return _remember_dependency_status("main_api", "unreachable")


async def check_browserless_dependency() -> str:
    if not settings.BROWSERLESS_URL:
        return _remember_dependency_status("browserless", "unconfigured")

    try:
        params = {}
        if settings.BROWSERLESS_TOKEN:
            params["token"] = settings.BROWSERLESS_TOKEN

        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as http:
            resp = await http.get(
                f"{settings.BROWSERLESS_URL}/pressure",
                params=params,
            )

        if resp.status_code == 200:
            return _remember_dependency_status("browserless", "connected")
        return _remember_dependency_status("browserless", f"degraded_{resp.status_code}")
    except httpx.TimeoutException:
        logger.warning("[HEALTH] Browserless health check timed out.")
        return _remember_dependency_status("browserless", "timeout")
    except Exception as e:
        logger.warning(f"[HEALTH] Browserless health check failed: {e}")
        return _remember_dependency_status("browserless", "unreachable")


# SCHEDULER SETUP
scheduler = AsyncIOScheduler()

# Semaphore to limit concurrent browser instances
CONCURRENCY_LIMIT = asyncio.Semaphore(3)



def ensure_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def cursor_to_list(cursor):
    return [doc async for doc in cursor]


async def check_single_source(source: dict):
    # Acquire semaphore
    async with CONCURRENCY_LIMIT:
        collections = get_async_scheduler_collections()
        source_id = source["_id"]
        name = source["name"]
        url = source["listing_url"]
        pattern = source.get("url_pattern")

        logger.info(f"🔎 Checking source: {name} ({url})")

        try:
            # 1. Fetch & Extract
            html = await fetch_listing_page(url)
            found_urls = extract_valid_urls(html, url, pattern)

            if not found_urls:
                logger.info(f"No URLs found for {name}.")
                await collections.sources.update_one(
                    {"_id": source_id},
                    {"$set": {"last_run_at": datetime.now(timezone.utc)}}
                )
                return

            # 2. Deduplicate
            existing_docs = collections.processed_articles.find(
                {"url": {"$in": list(found_urls)}},
                {"url": 1}
            )
            existing_urls = {doc["url"] async for doc in existing_docs}
            new_urls = found_urls - existing_urls

            logger.info(f"Found {len(found_urls)} links. {len(new_urls)} are new.")

            # 3. Submit Jobs
            async with httpx.AsyncClient() as http_client:
                for link in new_urls:
                    new_article = {
                        "_id": str(uuid.uuid4()),
                        "source_id": source_id,
                        "url": link,
                        "status": "queued",
                        "discovered_at": datetime.now(timezone.utc)
                    }
                    try:
                        await collections.processed_articles.insert_one(new_article)
                    except Exception:
                        continue

                    # PREPARE API REQUEST
                    api_base = getattr(settings, 'MAIN_API_URL', "http://api:8003")
                    api_url = f"{api_base}/submit-job"

                    # [FIX] ADD SECURITY HEADERS
                    headers = {}
                    if settings.NEWSAGENT_API_KEY:
                        headers["X-API-Key"] = settings.NEWSAGENT_API_KEY
                    # ----------------------------------

                    payload = {"source_url": link, "max_retries": 3}

                    try:
                        # [FIX] PASS HEADERS HERE
                        resp = await http_client.post(api_url, json=payload, headers=headers)
                        resp.raise_for_status()
                        logger.info(f"🚀 Submitted: {link}")
                    except Exception as e:
                        logger.error(f"❌ Failed to submit {link}: {e}")
                        await collections.processed_articles.update_one(
                            {"_id": new_article["_id"]},
                            {"$set": {"status": "submission_failed"}}
                        )

            # 4. Update Source Last Run
            await collections.sources.update_one(
                {"_id": source_id},
                {"$set": {"last_run_at": datetime.now(timezone.utc)}}
            )

        except Exception as e:
            error_msg = f"Error processing source {name}: {e}"
            logger.error(error_msg, exc_info=True)
            await send_error_email_async(
                job_id=f"scheduler-{source_id}",
                source_url=url,
                error_details=error_msg,
                traceback_info=traceback.format_exc()
            )

async def run_scheduler_cycle():
    """
    Main Loop: Finds active sources that are due for a check.
    """
    try:
        logger.info("⏰ Cycle starting...")
        collections = get_async_scheduler_collections()
        active_sources = collections.sources.find({"is_active": True})

        current_time = datetime.now(timezone.utc)

        async for source_doc in active_sources:
            last_run = ensure_utc(source_doc.get("last_run_at"))
            interval_mins = source_doc.get("fetch_interval_minutes", 60)

            should_run = False
            if not last_run:
                should_run = True
            else:
                delta = current_time - last_run
                if delta.total_seconds() / 60 >= interval_mins:
                    should_run = True

            if should_run:
                asyncio.create_task(check_single_source(source_doc))

    except Exception:
        logger.exception("Error in scheduler cycle")


# FASTAPI APP
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pinging MongoDB Atlas...")
    db_status = await check_database_dependency()
    if db_status == "connected":
        logger.info("MongoDB connection successful.")
    else:
        logger.error(f"MongoDB connection check failed: {db_status}")
        # Do not block startup. /health stays ALB-safe and /health/db or
        # /health/full exposes the live dependency state.

    scheduler.add_job(run_scheduler_cycle, IntervalTrigger(minutes=1))
    scheduler.start()
    logger.info("--- 🗓️ Scheduler Service Started ---")
    yield
    scheduler.shutdown()
    await close_async_governance_clients()
    await close_async_email_client()
    await close_async_mongo_client()
    close_legacy_mongo_client()


app = FastAPI(title="NewsAgent Scheduler & Archive", lifespan=lifespan, root_path="/newscheduler")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get(
    "/health",
    response_model=SchedulerStandardHealthResponse,
    description="ALB-safe liveness check. Does not perform live dependency checks.",
    tags=["System"],
)
async def health_check(response: Response):
    scheduler_state = _scheduler_status()
    overall = "healthy" if scheduler_state == "running" else "unhealthy"
    if overall != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return SchedulerStandardHealthResponse(
        status=overall,
        scheduler=scheduler_state,
        database=_last_known_dependency_status("database"),
        database_checked_at=_dependency_status_cache["database"]["checked_at"],
        timestamp=_utc_now(),
    )


@app.get(
    "/health/db",
    response_model=SchedulerDatabaseHealthResponse,
    description="Live database health check with a bounded non-blocking scheduler route.",
    tags=["System"],
)
async def database_health_check(response: Response):
    db_status = await check_database_dependency()
    overall = "healthy" if db_status == "connected" else "unhealthy"
    if overall != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return SchedulerDatabaseHealthResponse(
        status=overall,
        database=db_status,
        timestamp=_utc_now(),
    )


@app.get(
    "/health/full",
    response_model=SchedulerHealthResponse,
    description="Full operational dependency health check.",
    tags=["System"],
)
async def full_health_check(response: Response):
    db_status, redis_status, main_api_status, browserless_status = await asyncio.gather(
        check_database_dependency(),
        check_redis_dependency(),
        check_main_api_dependency(),
        check_browserless_dependency(),
    )
    scheduler_state = _scheduler_status()

    critical_statuses = [
        scheduler_state,
        db_status,
        redis_status,
        main_api_status,
        browserless_status,
    ]
    if any(
        value in {"stopped", "disconnected", "timeout", "unreachable", "unconfigured"}
        for value in critical_statuses
    ):
        overall = "unhealthy"
    elif any(str(value).startswith("degraded") for value in critical_statuses):
        overall = "degraded"
    else:
        overall = "healthy"

    if overall != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return SchedulerHealthResponse(
        status=overall,
        database=db_status,
        redis=redis_status,
        scheduler=scheduler_state,
        main_api=main_api_status,
        browserless=browserless_status,
        timestamp=_utc_now(),
    )

# 1. WEBHOOK ENDPOINT
@app.post(
    "/webhook/store-result",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Store the result of a job.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Result stored successfully",
        },
        status.HTTP_400_BAD_REQUEST: {
            "model": GenericResponse,
            "description": "Invalid Payload",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_webhook_secret)],
)
async def store_result(payload: Dict[str, Any]):
    url = payload.get("source_url")
    data = payload.get("data")

    if not url or not data:
        raise HTTPException(status_code=400, detail="Invalid Payload")

    logger.info(f"[WEBHOOK] 📥 Received result for: {url}")

    collections = get_async_scheduler_collections()
    result = await collections.processed_articles.update_one(
        {"url": url},
        {
            "$set": {
                "status": "processed",
                "processed_at": datetime.now(timezone.utc),
                "final_output": data,
            }
        },
    )

    if result.matched_count == 0:
        logger.info("[WEBHOOK] URL not in scheduler DB. Creating new record.")
        await collections.processed_articles.insert_one(
            {
                "_id": str(uuid.uuid4()),
                "source_id": "manual_submission",
                "url": url,
                "status": "processed",
                "discovered_at": datetime.now(timezone.utc),
                "processed_at": datetime.now(timezone.utc),
                "final_output": data,
            }
        )

    return {"status": "ok", "message": "Result stored"}


# 2. SOURCE MANAGEMENT ENDPOINTS
@app.post(
    "/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=GenericResponse,
    description="Add a new source.",
    responses={
        status.HTTP_201_CREATED: {
            "model": GenericResponse,
            "description": "Source added successfully",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def add_source(source: SourceConfig):
    collections = get_async_scheduler_collections()
    source_dict = source.dict(by_alias=True)
    if "created_at" in source_dict and source_dict["created_at"].tzinfo is None:
        source_dict["created_at"] = source_dict["created_at"].replace(
            tzinfo=timezone.utc
        )

    try:
        await collections.sources.insert_one(source_dict)
        return {
            "status": "created",
            "message": "Source added successfully",
            "id": source_dict["_id"],
        }
    except DuplicateKeyError as e:
        if "listing_url" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"A source with the listing url '{source.listing_url}' already exists."
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A source with this unique identifier already exists."
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@app.get(
    "/sources",
    response_model=List[SourceConfig],
    description="List all sources.",
    responses={
        status.HTTP_200_OK: {
            "model": List[SourceConfig],
            "description": "Sources retrieved successfully",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def list_sources():
    collections = get_async_scheduler_collections()
    return await cursor_to_list(collections.sources.find())


@app.get(
    "/sources/{source_id}",
    response_model=SourceConfig,
    description="Get a specific source.",
    responses={
        status.HTTP_200_OK: {
            "model": SourceConfig,
            "description": "Source retrieved successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Source not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def get_source(source_id: str):
    collections = get_async_scheduler_collections()
    source = await collections.sources.find_one({"_id": source_id})
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@app.patch(
    "/sources/{source_id}",
    response_model=GenericResponse,
    description="Update a specific source.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Source updated successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Source not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def update_source(source_id: str, updates: Dict[str, Any] = Body(...)):
    collections = get_async_scheduler_collections()
    if "_id" in updates:
        del updates["_id"]

    result = await collections.sources.update_one({"_id": source_id}, {"$set": updates})

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Source not found")

    return {"status": "updated", "message": "Source updated successfully", "id": source_id}


@app.post(
    "/sources/{source_id}/toggle",
    response_model=GenericResponse,
    description="Toggle the active status of a specific source.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Source status toggled successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Source not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def toggle_source_status(source_id: str):
    collections = get_async_scheduler_collections()
    source = await collections.sources.find_one({"_id": source_id})
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    new_status = not source.get("is_active", False)

    await collections.sources.update_one({"_id": source_id}, {"$set": {"is_active": new_status}})

    return {"status": "success", "message": f"Source status toggled to {'active' if new_status else 'inactive'}", "is_active": new_status}


@app.delete(
    "/sources/{source_id}",
    response_model=GenericResponse,
    description="Delete a specific source.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Source deleted successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Source not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def delete_source(source_id: str):
    collections = get_async_scheduler_collections()
    result = await collections.sources.delete_one({"_id": source_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Source not found")

    return {"status": "deleted", "message": "Source deleted successfully", "id": source_id}


@app.post(
    "/sources/{source_id}/run-now",
    response_model=GenericResponse,
    description="Manually trigger a check for a specific source immediately.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Source run triggered successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Source not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def trigger_source_run(source_id: str, background_tasks: BackgroundTasks):
    """
    Manually triggers a check for a specific source immediately,
    bypassing the time interval check.
    """
    # 1. Find the source
    collections = get_async_scheduler_collections()
    source = await collections.sources.find_one({"_id": source_id})
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    # 2. Add to Background Tasks
    # We use FastAPI's BackgroundTasks so the API returns immediately
    # while the crawler runs in the background.
    background_tasks.add_task(check_single_source, source)

    return {
        "status": "triggered",
        "message": f"Source '{source.get('name')}' queued for immediate check.",
    }


# 3. ARCHIVE ENDPOINTS
@app.get(
    "/articles",
    response_model=PaginatedArticleResponse,
    description="List all articles with pagination.",
    responses={
        status.HTTP_200_OK: {
            "model": PaginatedArticleResponse,
            "description": "Articles retrieved successfully",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def list_articles(
    page: int = Query(1, ge=1, description="Page number (starts at 1)"),
    limit: int = Query(50, ge=1, le=100, description="Number of items per page"),
    status: Optional[str] = None
):
    collections = get_async_scheduler_collections()
    query = {}
    if status:
        query["status"] = status

    # 1. Get Total Count
    total_count = await collections.processed_articles.count_documents(query)

    # 2. Calculate Skip
    skip = (page - 1) * limit

    # 3. Calculate Total Pages
    total_pages = (total_count + limit - 1) // limit

    # 4. Fetch Data
    cursor = collections.processed_articles.find(query).sort("discovered_at", -1).skip(skip).limit(limit)
    items = await cursor_to_list(cursor)

    return PaginatedArticleResponse(
        total=total_count,
        page=page,
        size=limit,
        pages=total_pages,
        items=items
    )


@app.get(
    "/articles/{article_id}",
    response_model=ProcessedArticle,
    description="Get a specific article.",
    responses={
        status.HTTP_200_OK: {
            "model": ProcessedArticle,
            "description": "Article retrieved successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Article not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def get_article(article_id: str):
    collections = get_async_scheduler_collections()
    article = await collections.processed_articles.find_one({"_id": article_id})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article

@app.patch(
    "/articles/{article_id}/status",
    response_model=GenericResponse,
    description="Update the status of a specific article.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Article status updated successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Article not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def update_article_status(
    article_id: str, status_update: Dict[str, str] = Body(...)
):
    collections = get_async_scheduler_collections()
    new_status = status_update.get("status")
    allowed_statuses = ["processed", "approved", "rejected", "duplicated"]

    if new_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {allowed_statuses}",
        )

    # Trigger embedding generation if approved
    if new_status == "approved":
        try:
            # 1. Fetch full article
            article = await collections.processed_articles.find_one({"_id": article_id})
            if not article:
                raise HTTPException(status_code=404, detail="Article not found")

            # 2. Normalize and Chunk
            logger.info(f"🧠 Generating embeddings for article: {article_id}")
            norm_doc = normalize_document(article)
            
            chunker = Chunker(chunk_size=1000, chunk_overlap=200)
            chunks = chunker.chunk_document(norm_doc)
            
            docs_to_ingest = []
            for chunk in chunks:
                summary = chunk.get("summary", "")
                title = chunk.get("title", "")
                content_part = chunk.get("content_chunk", "") # Chunker puts split text here
                url = chunk.get("url", "")
                
                # Composite text for embedding
                chunk_text = f"Summary: {summary}\nTitle: {title}\nContent: {content_part}\nURL: {url}"
                
                # Metadata
                metadata = {
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "published_date": chunk.get("published_date"),
                    "chunk_index": chunk.get("chunk_index"),
                    "original_metadata": chunk.get("original_metadata")
                }
                
                docs_to_ingest.append({
                    "source_id": chunk.get("source_id"),
                    "chunk_text": chunk_text,
                    "metadata": metadata
                })

            # 3. Store in Vector DB
            if docs_to_ingest:
                mongo_store = get_mongo_store()
                await mongo_store.add_documents(docs_to_ingest)
                # Note: mongo_store connection is managed globally/singleton, so strict close per request isn't typical here 
                # unless we want to force cleanup, but get_mongo_store reuses the client.
                logger.info(f"✅ Embeddings stored for {article_id} ({len(docs_to_ingest)} chunks)")
            else:
                logger.warning(f"⚠️ No content to chunk for {article_id}")

        except Exception as e:
            logger.error(f"❌ Error generating embeddings: {e}", exc_info=True)
            # We catch exception so status update still proceeds, or should we fail?
            # Usually better to fail if strict, but maybe logging is enough. 
            # Let's log and proceed for now, or raise 500? 
            # Plan didn't specify, but safer to let user know it failed.
            # However, if we fail here, status isn't updated. 
            # I will allow status update to proceed but log error strongly.

    result = await collections.processed_articles.update_one(
        {"_id": article_id}, {"$set": {"status": new_status}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Article not found")

    return {"status": "updated", "message": f"Article status updated to {new_status}", "id": article_id, "new_status": new_status}


@app.patch(
    "/articles/{article_id}/image",
    response_model=GenericResponse,
    description="Upload a new image to S3 (renamed to article title) and update MongoDB (Image & SEO).",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Article image updated successfully",
        },
        status.HTTP_400_BAD_REQUEST: {
            "model": GenericResponse,
            "description": "Invalid file type",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Article not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def update_article_image(
    article_id: str, 
    file: UploadFile = File(...)
):
    """
    Updates the 'top_image' and the SEO 'mainEntityOfPage' URL.
    """
    # 1. Validate S3 Configuration
    if not settings.AWS_ACCESS_KEY_ID or not settings.S3_BUCKET_NAME:
        raise HTTPException(
            status_code=500, 
            detail="Server S3 configuration is missing."
        )

    # 2. Validate File Content (Strict)
    ALLOWED_MIME_TYPES = {
        "image/jpeg": ".jpg", 
        "image/png": ".png", 
        "image/webp": ".webp"
    }
    
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {file.content_type}. Only JPEG, PNG, and WEBP are allowed."
        )

    # 3. Fetch Article (Needed for SEO Title)
    collections = get_async_scheduler_collections()
    article = await collections.processed_articles.find_one({"_id": article_id})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    try:
        # 4. Generate SEO Filename
        title = article.get("final_output", {}).get("title")
        
        if not title:
            # Fallback: Use the original filename if title is missing
            title = file.filename.rsplit('.', 1)[0]

        # Sanitize Title for S3 Key
        clean_title = re.sub(r'[^a-zA-Z0-9\s-]', '', title.lower())
        clean_title = re.sub(r'[-\s]+', '-', clean_title).strip('-')
        
        # Temp file handling for compression
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp_input:
            # Write uploaded content to temp file
            await asyncio.to_thread(shutil.copyfileobj, file.file, tmp_input)
            tmp_input_path = tmp_input.name
        
        compressed_path = None
        final_file_path = tmp_input_path
        final_content_type = file.content_type
        # Default extension based on input, but might change if compressed
        final_extension = ALLOWED_MIME_TYPES[file.content_type]

        try:
            # Attempt Compression
            logger.info(f"Compressing image: {tmp_input_path}")
            # Target 1.5MB as per default
            result = await asyncio.to_thread(compress_image, tmp_input_path, target_size_mb=1.5)
            
            if result['success']:
                compressed_path = result['output_path']
                final_file_path = compressed_path
                # Compressor converts to JPEG
                final_content_type = "image/jpeg"
                final_extension = ".jpg"
                logger.info(f"Compression successful. New size: {result['compressed_size_mb']}MB")
            else:
                logger.warning(f"Compression failed/skipped: {result.get('error')}. Using original.")
                
            # Recalculate S3 Key with potentially new extension
            key_path = f"articles/{article_id}/{clean_title}{final_extension}"
            s3_key = key_path
            
            if settings.S3_FOLDER_PREFIX:
                prefix = settings.S3_FOLDER_PREFIX.strip("/")
                if prefix:
                    s3_key = f"{prefix}/{key_path}"

            # 5. Upload to S3
            def upload_to_s3():
                s3_client = boto3.client(
                    's3',
                    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                    region_name=settings.AWS_REGION
                )

                with open(final_file_path, "rb") as f:
                    s3_client.upload_fileobj(
                        f,
                        settings.S3_BUCKET_NAME,
                        s3_key,
                        ExtraArgs={'ContentType': final_content_type}
                    )

            await asyncio.to_thread(upload_to_s3)

            # 6. Construct Public URLs
            new_image_url = f"https://{settings.S3_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/{s3_key}"
            
            # Construct the Internal Canonical URL
            new_canonical_url = f"https://propt.global/news/{article_id}"

            # 7. Update Database (3 Fields Updated)
            update_op = {
                "$set": {
                    # 1. Update the visual image reference
                    "final_output.top_image": new_image_url,
                    
                    # 2. Update the SEO Schema image reference
                    "final_output.seo.json_ld_schema.image": new_image_url,
                    
                    # 3. Update the Canonical ID to point to your platform
                    "final_output.seo.json_ld_schema.mainEntityOfPage.@id": new_canonical_url
                }
            }

            result = await collections.processed_articles.update_one(
                {"_id": article_id}, 
                update_op
            )

            return {
                "status": "updated",
                "message": "Article image and SEO URLs updated successfully",
                "id": article_id,
                "new_image_url": new_image_url,
                "new_canonical_url": new_canonical_url
            }

        finally:
            # Cleanup temp files
            if os.path.exists(tmp_input_path):
                try:
                    os.unlink(tmp_input_path)
                except Exception:
                    pass
            # If compressed path is different and exists (though compress_image overwrites by default logic if not specified, 
            # let's be safe if logic changes)
            if compressed_path and compressed_path != tmp_input_path and os.path.exists(compressed_path):
                try:
                    os.unlink(compressed_path)
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"S3 Upload Error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to upload image: {str(e)}"
        )
    finally:
        await file.close()

@app.patch(
    "/articles/{article_id}/title",
    dependencies=[Depends(verify_api_key)]
)
async def update_article_title(article_id: str, title_update: Dict[str, str] = Body(...)):
    """
    Updates the title of a processed article.
    Expects a JSON payload: {"title": "New Updated Title"}
    """
    new_title = title_update.get("title")

    if not new_title or not new_title.strip():
        raise HTTPException(
            status_code=400,
            detail="Title cannot be empty."
        )

    # Sanitize Title for Slug
    clean_title = re.sub(r'[^a-zA-Z0-9\s-]', '', new_title.lower())
    new_slug = re.sub(r'[-\s]+', '-', clean_title).strip('-')

    # The title is stored inside the 'final_output' object
    collections = get_async_scheduler_collections()
    result = await collections.processed_articles.update_one(
        {"_id": article_id},
        {"$set": {
            "final_output.title": new_title.strip(),
            "final_output.seo.slug": new_slug
        }}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Article not found")

    return {"status": "updated", "id": article_id, "new_title": new_title.strip(), "new_slug": new_slug}

@app.get(
    "/articles/search/text",
    response_model=PaginatedArticleResponse,
    description="Dedicated API for standard text search across articles, including full content.",
    responses={
        status.HTTP_200_OK: {
            "model": PaginatedArticleResponse,
            "description": "Search results retrieved successfully",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def search_articles_text(
    q: str = Query(..., min_length=1, description="The text/keyword to search for"),
    page: int = Query(1, ge=1, description="Page number (starts at 1)"),
    limit: int = Query(50, ge=1, le=100, description="Number of items per page"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status")
):
    """
    Standard Regex Search. Looks for the keyword in the URL, Title, Summary, and Full Content.
    """
    try:
        collections = get_async_scheduler_collections()
        query = {}
        if status_filter:
            query["status"] = status_filter

        # Case-insensitive search across title, summary, url, and full content
        query["$or"] = [
            {"final_output.title": {"$regex": q, "$options": "i"}},
            {"final_output.summary": {"$regex": q, "$options": "i"}},
            {"url": {"$regex": q, "$options": "i"}},
            {"final_output.content": {"$regex": q, "$options": "i"}},    # <-- Added English Content
            {"final_output.content_ar": {"$regex": q, "$options": "i"}} # <-- Added Arabic Content (optional but helpful)
        ]

        # 1. Get Total Count
        total_count = await collections.processed_articles.count_documents(query)

        # 2. Calculate Skip
        skip = (page - 1) * limit

        # 3. Calculate Total Pages
        total_pages = (total_count + limit - 1) // limit if limit > 0 else 0

        # 4. Fetch Data
        cursor = collections.processed_articles.find(query).sort("discovered_at", -1).skip(skip).limit(limit)
        items = await cursor_to_list(cursor)

        return PaginatedArticleResponse(
            total=total_count,
            page=page,
            size=limit,
            pages=total_pages,
            items=items
        )
    except Exception as e:
        logger.error(f"Text search failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get(
    "/articles/search/similarity",
    description="Dedicated API for semantic/AI search to find similar articles or duplicates.",
    responses={
        status.HTTP_200_OK: {
            "description": "Semantic search completed",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def search_articles_similarity(
    q: str = Query(..., min_length=5, description="The semantic query or paragraph to match against"),
    limit: int = Query(5, ge=1, le=50, description="Max similar results to return"),
    min_score: float = Query(0.7, ge=0.0, le=1.0, description="Minimum relevance score (cosine similarity)")
):
    """
    Vector Search / AI Similarity. Takes a text snippet, embeds it using OpenAI, 
    and finds contextually similar article chunks in the database.
    """
    try:
        mongo_store = get_mongo_store()
        
        # Execute vector search via MongoStore
        # Note: Your MongoStore query method returns chunks, not full articles
        results = await mongo_store.query(query_text=q, limit=limit)
        
        formatted_results = []
        for res in results:
            score = res.get("score", 0.0)
            
            # Optionally filter out weak matches
            if score >= min_score:
                formatted_results.append({
                    "source_article_id": res.get("source_id"),
                    "matching_chunk": res.get("chunk_text"),
                    "relevance_score": score,
                    "metadata": res.get("metadata", {})
                })
                
        return {
            "status": "success",
            "query": q,
            "total_matches": len(formatted_results),
            "results": formatted_results
        }
    except Exception as e:
        logger.error(f"Similarity search failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# 4. ARTICLE LIFECYCLE MANAGEMENT
@app.post(
    "/articles/{article_id}/archive",
    response_model=GenericResponse,
    description="Archive a specific article.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Article archived successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Article not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def archive_article(article_id: str):
    """
    Moves an article from 'processed_articles' to 'archived_articles'.
    Typically used for successfully processed items.
    """
    # 1. Find the article
    collections = get_async_scheduler_collections()
    article = await collections.processed_articles.find_one({"_id": article_id})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found in active list")

    # 2. Insert into Archive
    # Add a metadata field for when it was archived
    article["archived_at"] = datetime.now(timezone.utc)
    try:
        await collections.archived_articles.insert_one(article)
    except Exception as e:
        # If it already exists in archive, strictly speaking we can proceed to delete,
        # but let's warn if it's a real error.
        if "duplicate key" not in str(e).lower():
            raise HTTPException(status_code=500, detail=f"Failed to archive: {str(e)}")

    # 3. Delete from Active
    await collections.processed_articles.delete_one({"_id": article_id})

    return {"status": "archived", "message": "Article archived successfully", "id": article_id}


@app.delete(
    "/articles/{article_id}",
    response_model=GenericResponse,
    description="Soft delete a specific article.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Article soft deleted successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Article not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def soft_delete_article(article_id: str):
    """
    SOFT DELETE: Moves an article from 'processed_articles' to 'deleted_articles'.
    Typically used for queued, failed, or unwanted items.
    """
    # 1. Find the article
    collections = get_async_scheduler_collections()
    article = await collections.processed_articles.find_one({"_id": article_id})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found in active list")

    # 2. Insert into Deleted Table
    article["deleted_at"] = datetime.now(timezone.utc)
    try:
        await collections.deleted_articles.insert_one(article)
    except Exception as e:
        if "duplicate key" not in str(e).lower():
            raise HTTPException(
                status_code=500, detail=f"Failed to move to trash: {str(e)}"
            )

    # 3. Delete from Active
    await collections.processed_articles.delete_one({"_id": article_id})

    return {"status": "soft_deleted", "message": "Article soft deleted successfully", "id": article_id}


# 5. OPS / BACKFILL ENDPOINTS
@app.post(
    "/admin/ops/backfill-reading-time",
    response_model=GenericResponse,
    description="Trigger a backfill of reading_time for existing articles.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Backfill triggered successfully",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def trigger_backfill_reading_time(background_tasks: BackgroundTasks):
    """
    Triggers a background task to calculate and update 'reading_time' for all processed articles.
    This is useful for migrating old data to the new schema.
    """
    background_tasks.add_task(run_reading_time_backfill)
    return {"status": "triggered", "message": "Reading time backfill started in background."}


async def run_reading_time_backfill():
    import math
    logger.info("[OPS] 🔄 Starting Reading Time Backfill...")
    try:
        collections = get_async_scheduler_collections()
        cursor = collections.processed_articles.find({"final_output": {"$ne": None}})
        
        updated_count = 0
        skipped_count = 0
        wpm = 200

        async for doc in cursor:
            article_id = doc["_id"]
            final_output = doc.get("final_output", {})
            
            # Skip if already fully populated
            if final_output.get("reading_time") and final_output.get("reading_time_ar"):
                skipped_count += 1
                continue

            updates = {}
            
            # Calc English
            if not final_output.get("reading_time"):
                content_en = final_output.get("cleaned_article_text") or final_output.get("content")
                if content_en:
                    word_count = len(content_en.split())
                    updates["final_output.reading_time"] = math.ceil(word_count / wpm)

            # Calc Arabic
            if not final_output.get("reading_time_ar"):
                content_ar = final_output.get("content_ar")
                if content_ar:
                    word_count_ar = len(content_ar.split())
                    updates["final_output.reading_time_ar"] = math.ceil(word_count_ar / wpm)

            if updates:
                await collections.processed_articles.update_one({"_id": article_id}, {"$set": updates})
                updated_count += 1
            else:
                skipped_count += 1
        
        logger.info(f"[OPS] ✅ Backfill Complete. Updated: {updated_count}, Skipped: {skipped_count}")

    except Exception as e:
        logger.error(f"[OPS] ❌ Backfill Failed: {e}", exc_info=True)


if __name__ == "__main__":
    import uvicorn

    # uuid is now imported at top
    uvicorn.run(app, host="0.0.0.0", port=8001)
