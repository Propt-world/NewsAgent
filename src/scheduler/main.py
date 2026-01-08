import asyncio
import os
import traceback
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    HTTPException,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pymongo import MongoClient
from bson import ObjectId
import httpx

from src.configs.settings import settings
from src.scheduler.models import SourceConfig, ProcessedArticle
from src.scheduler.link_discovery import fetch_listing_page, extract_valid_urls
from src.utils.email_utils import send_error_email
from src.utils.security import verify_api_key, verify_webhook_secret
from src.models.Responses import GenericResponse, SchedulerHealthResponse

# --- DATABASE SETUP ---
client = MongoClient(settings.DATABASE_URL, tlsInsecure=True)
db = client[settings.MONGO_DB_NAME]
sources_col = db["sources"]
articles_col = db["processed_articles"]
archive_col = db["archived_articles"]
deleted_col = db["deleted_articles"]

# --- SCHEDULER SETUP ---
scheduler = AsyncIOScheduler()

# Semaphore to limit concurrent browser instances
CONCURRENCY_LIMIT = asyncio.Semaphore(3)


def ensure_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def check_single_source(source: dict):
    # Acquire semaphore
    async with CONCURRENCY_LIMIT:
        source_id = source["_id"]
        name = source["name"]
        url = source["listing_url"]
        pattern = source.get("url_pattern")

        print(f"[SCHEDULER] 🔎 Checking source: {name} ({url})")

        try:
            # 1. Fetch & Extract
            html = await fetch_listing_page(url)
            found_urls = extract_valid_urls(html, url, pattern)

            if not found_urls:
                print(f"[SCHEDULER] No URLs found for {name}.")
                sources_col.update_one(
                    {"_id": source_id},
                    {"$set": {"last_run_at": datetime.now(timezone.utc)}}
                )
                return

            # 2. Deduplicate
            existing_docs = articles_col.find(
                {"url": {"$in": list(found_urls)}},
                {"url": 1}
            )
            existing_urls = {doc["url"] for doc in existing_docs}
            new_urls = found_urls - existing_urls

            print(f"[SCHEDULER] Found {len(found_urls)} links. {len(new_urls)} are new.")

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
                        articles_col.insert_one(new_article)
                    except Exception:
                        continue

                    # --- PREPARE API REQUEST ---
                    api_base = getattr(settings, 'MAIN_API_URL', "http://api:8000")
                    api_url = f"{api_base}/submit-job"

                    # --- [FIX] ADD SECURITY HEADERS ---
                    headers = {}
                    if settings.NEWSAGENT_API_KEY:
                        headers["X-API-Key"] = settings.NEWSAGENT_API_KEY
                    # ----------------------------------

                    payload = {"source_url": link, "max_retries": 3}

                    try:
                        # --- [FIX] PASS HEADERS HERE ---
                        resp = await http_client.post(api_url, json=payload, headers=headers)
                        resp.raise_for_status()
                        print(f"[SCHEDULER] 🚀 Submitted: {link}")
                    except Exception as e:
                        print(f"[SCHEDULER] ❌ Failed to submit {link}: {e}")
                        articles_col.update_one(
                            {"_id": new_article["_id"]},
                            {"$set": {"status": "submission_failed"}}
                        )

            # 4. Update Source Last Run
            sources_col.update_one(
                {"_id": source_id},
                {"$set": {"last_run_at": datetime.now(timezone.utc)}}
            )

        except Exception as e:
            error_msg = f"Error processing source {name}: {e}"
            print(f"[SCHEDULER] {error_msg}")
            traceback.print_exc()
            send_error_email(
                job_id=f"scheduler-{source_id}",
                source_url=url,
                error_details=error_msg,
                traceback_info=traceback.format_exc()
            )

async def run_scheduler_cycle():
    """
    Main Loop: Finds active sources that are due for a check.
    """
    print("[SCHEDULER] ⏰ Cycle starting...")
    active_sources = sources_col.find({"is_active": True})

    current_time = datetime.now(timezone.utc)

    for source_doc in active_sources:
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


# --- FASTAPI APP ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(run_scheduler_cycle, IntervalTrigger(minutes=1))
    scheduler.start()
    print("--- 🗓️ Scheduler Service Started ---")
    yield
    scheduler.shutdown()


app = FastAPI(title="NewsAgent Scheduler & Archive", lifespan=lifespan, root_path="/newscheduler")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://main.d211u21suwdysn.amplifyapp.com", "http://localhost:3000", "http://localhost:8001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get(
    "/health",
    response_model=SchedulerHealthResponse,
    description="Check the health of the scheduler service and its dependencies.",
    tags=["System"],
)
async def health_check(check_external: bool = True):
    # 1. Database Check
    db_status = "disconnected"
    try:
        # Using the global 'client' to ping
        client.admin.command('ping')
        db_status = "connected"
    except Exception:
        db_status = "disconnected"

    # 2. Scheduler Check
    sched_status = "stopped"
    if scheduler.running:
        sched_status = "running"

    # 3. Main API Check
    api_status = "skipped"
    if check_external:
        api_status = "unreachable"
        try:
            async with httpx.AsyncClient(timeout=3.0) as http:
                # Attempt to hit the Main API health endpoint with recursion breaker
                resp = await http.get(f"{settings.MAIN_API_URL}/health", params={"check_external": "false"})
                if resp.status_code == 200:
                    api_status = "connected"
                else:
                    api_status = f"degraded ({resp.status_code})"
        except Exception:
            api_status = "unreachable"

    # 4. Browserless Check
    browser_status = "unconfigured"
    if settings.BROWSERLESS_URL:
        # Browserless is a leaf dependency (doesn't check us), so we can always check it,
        # but for speed in 'shallow' mode we could skip it too. 
        # However, check_external usually implies "services that depend on us or we depend on in a cycle".
        # Browserless is a pure dependency. Let's keep verifying it unless strict speed is needed.
        # Check if browserless is valid
        try:
            url = f"{settings.BROWSERLESS_URL}/pressure"
            params = {}
            if settings.BROWSERLESS_TOKEN:
                params["token"] = settings.BROWSERLESS_TOKEN

            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.get(url, params=params)
                if resp.status_code == 200:
                    browser_status = "connected"
                else:
                    browser_status = f"degraded ({resp.status_code})"
        except Exception:
            browser_status = "unreachable"

    # Overall Status
    # Any major component failure = unhealthy
    # We now count browserless as a major component
    if db_status != "connected" or sched_status != "running" or browser_status == "unreachable":
        # Note: browserless "unconfigured" is technically healthy-ish if optional, but here we treat it as mandatory per plan.
        # But if unconfigured is NOT possible due to settings choice, we assume mandatory.
        # If browser_status is 'degraded', we might still say healthy or degraded.
        overall = "unhealthy"
    elif api_status == "unreachable":
        # If Main API is down, Scheduler is degraded but still running
        overall = "degraded"
    else:
        overall = "healthy"
    
    # If skipping external, we shouldn't mark as degraded just because api_status is 'skipped'
    if check_external is False and api_status == "skipped":
        # If everything else is fine
        if overall == "degraded": 
            # Was degraded only due to api?
            # Re-evaluate
            if db_status == "connected" and sched_status == "running" and browser_status != "unreachable":
                overall = "healthy"

    return SchedulerHealthResponse(
        status=overall,
        database=db_status,
        scheduler=sched_status,
        main_api=api_status,
        browserless=browser_status,
        timestamp=datetime.now(timezone.utc)
    )

# --- 1. WEBHOOK ENDPOINT ---
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

    print(f"[WEBHOOK] 📥 Received result for: {url}")

    result = articles_col.update_one(
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
        print("[WEBHOOK] URL not in scheduler DB. Creating new record.")
        articles_col.insert_one(
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


# --- 2. SOURCE MANAGEMENT ENDPOINTS ---


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
    source_dict = source.dict(by_alias=True)
    if "created_at" in source_dict and source_dict["created_at"].tzinfo is None:
        source_dict["created_at"] = source_dict["created_at"].replace(
            tzinfo=timezone.utc
        )

    try:
        sources_col.insert_one(source_dict)
        return {
            "status": "created",
            "message": "Source added successfully",
            "id": source_dict["_id"],
        }
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
    return list(sources_col.find())


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
    source = sources_col.find_one({"_id": source_id})
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
    if "_id" in updates:
        del updates["_id"]

    result = sources_col.update_one({"_id": source_id}, {"$set": updates})

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
    source = sources_col.find_one({"_id": source_id})
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    new_status = not source.get("is_active", False)

    sources_col.update_one({"_id": source_id}, {"$set": {"is_active": new_status}})

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
    result = sources_col.delete_one({"_id": source_id})

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
    source = sources_col.find_one({"_id": source_id})
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


# --- 3. ARCHIVE ENDPOINTS ---
@app.get(
    "/articles",
    response_model=List[ProcessedArticle],
    description="List all articles.",
    responses={
        status.HTTP_200_OK: {
            "model": List[ProcessedArticle],
            "description": "Articles retrieved successfully",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def list_articles(limit: int = 50, skip: int = 0, status: Optional[str] = None):
    query = {}
    if status:
        query["status"] = status

    cursor = articles_col.find(query).sort("discovered_at", -1).skip(skip).limit(limit)
    return list(cursor)


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
    article = articles_col.find_one({"_id": article_id})
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
    new_status = status_update.get("status")
    allowed_statuses = ["processed", "approved", "rejected", "duplicated"]

    if new_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {allowed_statuses}",
        )

    result = articles_col.update_one(
        {"_id": article_id}, {"$set": {"status": new_status}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Article not found")

    return {"status": "updated", "message": f"Article status updated to {new_status}", "id": article_id, "new_status": new_status}


@app.patch(
    "/articles/{article_id}/image",
    response_model=GenericResponse,
    description="Update the top_image of a processed article.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Article image updated successfully",
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
    article_id: str, image_update: Dict[str, str] = Body(...)
):
    """
    Updates the 'top_image' field in the 'final_output' of a processed article.
    Expects JSON body: { "image_url": "https://..." }
    """
    new_image_url = image_update.get("image_url")
    if not new_image_url:
         raise HTTPException(
            status_code=400,
            detail="Missing 'image_url' in request body."
        )

    # We need to update two places potentially:
    # 1. final_output.top_image
    # 2. final_output.news_article.top_image (if the structure is nested as expected)
    
    # We use dot notation for Mongo updates to reach into the dict
    update_op = {
        "$set": {
            "final_output.top_image": new_image_url
        }
    }

    result = articles_col.update_one(
        {"_id": article_id}, 
        update_op
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Article not found")

    return {
        "status": "updated",
        "message": "Article image updated successfully",
        "id": article_id,
        "new_image_url": new_image_url
    }


# --- 4. ARTICLE LIFECYCLE MANAGEMENT ---


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
    article = articles_col.find_one({"_id": article_id})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found in active list")

    # 2. Insert into Archive
    # Add a metadata field for when it was archived
    article["archived_at"] = datetime.now(timezone.utc)
    try:
        archive_col.insert_one(article)
    except Exception as e:
        # If it already exists in archive, strictly speaking we can proceed to delete,
        # but let's warn if it's a real error.
        if "duplicate key" not in str(e).lower():
            raise HTTPException(status_code=500, detail=f"Failed to archive: {str(e)}")

    # 3. Delete from Active
    articles_col.delete_one({"_id": article_id})

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
    article = articles_col.find_one({"_id": article_id})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found in active list")

    # 2. Insert into Deleted Table
    article["deleted_at"] = datetime.now(timezone.utc)
    try:
        deleted_col.insert_one(article)
    except Exception as e:
        if "duplicate key" not in str(e).lower():
            raise HTTPException(
                status_code=500, detail=f"Failed to move to trash: {str(e)}"
            )

    # 3. Delete from Active
    articles_col.delete_one({"_id": article_id})

    return {"status": "soft_deleted", "message": "Article soft deleted successfully", "id": article_id}


# --- 5. OPS / BACKFILL ENDPOINTS ---

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


def run_reading_time_backfill():
    import math
    print("[OPS] 🔄 Starting Reading Time Backfill...")
    try:
        # Re-use global 'articles_col'
        cursor = articles_col.find({"final_output": {"$ne": None}})
        
        updated_count = 0
        skipped_count = 0
        wpm = 200

        for doc in cursor:
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
                articles_col.update_one({"_id": article_id}, {"$set": updates})
                updated_count += 1
            else:
                skipped_count += 1
        
        print(f"[OPS] ✅ Backfill Complete. Updated: {updated_count}, Skipped: {skipped_count}")

    except Exception as e:
        print(f"[OPS] ❌ Backfill Failed: {e}")


if __name__ == "__main__":
    import uvicorn

    # uuid is now imported at top
    uvicorn.run(app, host="0.0.0.0", port=8001)
