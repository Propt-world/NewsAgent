import asyncio
import os
import traceback
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Body
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pymongo import MongoClient
from bson import ObjectId
import httpx

from src.configs.settings import settings
from src.scheduler.models import SourceConfig, ProcessedArticle
from src.scheduler.link_discovery import fetch_listing_page, extract_valid_urls
from src.utils.email_utils import send_error_email # Import email utility

# --- DATABASE SETUP ---
client = MongoClient(settings.DATABASE_URL)
db = client[settings.MONGO_DB_NAME]
sources_col = db["sources"]
articles_col = db["processed_articles"]

# --- SCHEDULER SETUP ---
# We use a concise interval for the main loop (e.g., 1 minute)
# This allows us to catch "5 minute" interval sources accurately.
scheduler = AsyncIOScheduler()

async def check_single_source(source: dict):
    """
    Task to process a single source:
    1. Fetch HTML
    2. Extract Links
    3. Filter Duplicates
    4. Submit New Jobs to Main API
    """
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
            # Even if no URLs, we update last_run_at so we don't retry immediately
            sources_col.update_one(
                {"_id": source_id},
                {"$set": {"last_run_at": datetime.now(timezone.utc)}}
            )
            return

        # 2. Deduplicate against DB
        existing_docs = articles_col.find(
            {"url": {"$in": list(found_urls)}},
            {"url": 1}
        )
        existing_urls = {doc["url"] for doc in existing_docs}

        new_urls = found_urls - existing_urls

        print(f"[SCHEDULER] Found {len(found_urls)} links. {len(new_urls)} are new.")

        # 3. Submit Jobs for New URLs
        async with httpx.AsyncClient() as http_client:
            for link in new_urls:
                # A. Create Record in DB (Status: queued)
                new_article = {
                    "_id": str(uuid.uuid4()),
                    "source_id": source_id,
                    "url": link,
                    "status": "queued",
                    "discovered_at": datetime.now(timezone.utc)
                }
                # Use try/except for duplicate key error (race condition safety)
                try:
                    articles_col.insert_one(new_article)
                except Exception:
                    continue # Skip if already exists

                # B. Call Main API to start extraction
                api_url = f"{settings.MAIN_API_URL}/submit-job"

                payload = {
                    "source_url": link,
                    "max_retries": 3
                }

                try:
                    resp = await http_client.post(api_url, json=payload)
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

        # --- EMAIL NOTIFICATION FOR DISCOVERY FAILURE ---
        # We catch exceptions here (like 404, DNS error, parsing error)
        # and notify the admin.
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
        last_run = source_doc.get("last_run_at")
        interval_mins = source_doc.get("fetch_interval_minutes", 60)

        # Logic: If never run OR (now - last_run) > interval
        should_run = False
        if not last_run:
            should_run = True
        else:
            delta = current_time - last_run
            if delta.total_seconds() / 60 >= interval_mins:
                should_run = True

        if should_run:
            # We launch this as a background task so we don't block the loop
            asyncio.create_task(check_single_source(source_doc))

# --- FASTAPI APP ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run the cycle every 1 minute to ensure high precision for 5-min intervals
    scheduler.add_job(run_scheduler_cycle, IntervalTrigger(minutes=1))
    scheduler.start()
    print("--- 🗓️ Scheduler Service Started ---")
    yield
    scheduler.shutdown()

app = FastAPI(title="NewsAgent Scheduler & Archive", lifespan=lifespan)

# --- 1. WEBHOOK ENDPOINT (For the Worker) ---
@app.post("/webhook/store-result")
async def store_result(payload: Dict[str, Any]):
    """
    Receives the final payload from the Main Workflow.
    Updates status to 'processed' (default state for review).
    """
    url = payload.get("source_url")
    data = payload.get("data")

    if not url or not data:
        raise HTTPException(status_code=400, detail="Invalid Payload")

    print(f"[WEBHOOK] 📥 Received result for: {url}")

    # Update to 'processed' instead of 'completed'
    result = articles_col.update_one(
        {"url": url},
        {"$set": {
            "status": "processed",
            "processed_at": datetime.now(timezone.utc),
            "final_output": data
        }}
    )

    if result.matched_count == 0:
        print("[WEBHOOK] URL not in scheduler DB. Creating new record.")
        articles_col.insert_one({
            "_id": str(uuid.uuid4()),
            "source_id": settings.SUBMISSION_SOURCE_ID,
            "url": url,
            "status": "processed",
            "discovered_at": datetime.now(timezone.utc),
            "processed_at": datetime.now(timezone.utc),
            "final_output": data
        })

    return {"status": "ok", "message": "Result stored"}

# --- 2. SOURCE MANAGEMENT ENDPOINTS ---

@app.post("/sources", status_code=201)
async def add_source(source: SourceConfig):
    """Add a new news source."""
    source_dict = source.dict(by_alias=True)
    try:
        sources_col.insert_one(source_dict)
        return {"status": "created", "id": source_dict["_id"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sources")
async def list_sources():
    """List all sources."""
    return list(sources_col.find())

@app.get("/sources/{source_id}")
async def get_source(source_id: str):
    """Get a single source by ID."""
    source = sources_col.find_one({"_id": source_id})
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source

@app.patch("/sources/{source_id}")
async def update_source(source_id: str, updates: Dict[str, Any] = Body(...)):
    """
    Update specific fields of a source (e.g. name, url_pattern, interval).
    """
    if "_id" in updates:
        del updates["_id"]

    result = sources_col.update_one(
        {"_id": source_id},
        {"$set": updates}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Source not found")

    return {"status": "updated", "id": source_id}

@app.post("/sources/{source_id}/toggle")
async def toggle_source_status(source_id: str):
    """
    Toggle a source between active/inactive.
    """
    source = sources_col.find_one({"_id": source_id})
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    new_status = not source.get("is_active", False)

    sources_col.update_one(
        {"_id": source_id},
        {"$set": {"is_active": new_status}}
    )

    return {"status": "success", "is_active": new_status}

@app.delete("/sources/{source_id}")
async def delete_source(source_id: str):
    """
    Delete a source configuration.
    Does NOT delete the historical articles associated with it.
    """
    result = sources_col.delete_one({"_id": source_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Source not found")

    return {"status": "deleted", "id": source_id}

# --- 3. ARCHIVE ENDPOINTS ---
@app.get("/articles")
async def list_articles(
    limit: int = 50,
    skip: int = 0,
    status: Optional[str] = None
):
    """
    List archived articles with pagination.
    Optional filter by status (processed, approved, rejected, duplicated).
    """
    query = {}
    if status:
        query["status"] = status

    cursor = articles_col.find(query).sort("discovered_at", -1).skip(skip).limit(limit)
    return list(cursor)

@app.get("/articles/{article_id}")
async def get_article(article_id: str):
    """Get a single article by ID."""
    article = articles_col.find_one({"_id": article_id})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article

@app.patch("/articles/{article_id}/status")
async def update_article_status(article_id: str, status_update: Dict[str, str] = Body(...)):
    """
    Update the status of an article.
    Allowed statuses: processed, approved, rejected, duplicated.
    """
    new_status = status_update.get("status")
    allowed_statuses = ["processed", "approved", "rejected", "duplicated"]

    if new_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {allowed_statuses}"
        )

    result = articles_col.update_one(
        {"_id": article_id},
        {"$set": {"status": new_status}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Article not found")

    return {"status": "updated", "id": article_id, "new_status": new_status}

if __name__ == "__main__":
    import uvicorn
    import uuid
    uvicorn.run(app, host="0.0.0.0", port=8001)