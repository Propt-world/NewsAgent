import asyncio
import os
import traceback
import uuid  # <--- CRITICAL FIX: Added this import
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Query
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pymongo import MongoClient
from bson import ObjectId
import httpx

from src.configs.settings import settings
from src.scheduler.models import SourceConfig, ProcessedArticle
from src.scheduler.link_discovery import fetch_listing_page, extract_valid_urls
from src.utils.email_utils import send_error_email
from src.utils.log_viewer import get_container_logs, format_logs_html

# --- DATABASE SETUP ---
client = MongoClient(settings.DATABASE_URL)
db = client[settings.MONGO_DB_NAME]
sources_col = db["sources"]
articles_col = db["processed_articles"]

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
                        "_id": str(uuid.uuid4()), # This line was crashing before
                        "source_id": source_id,
                        "url": link,
                        "status": "queued",
                        "discovered_at": datetime.now(timezone.utc)
                    }
                    try:
                        articles_col.insert_one(new_article)
                    except Exception:
                        continue

                    api_url = f"{settings.MAIN_API_URL}/submit-job" if hasattr(settings, 'MAIN_API_URL') else "http://api:8000/submit-job"

                    payload = {"source_url": link, "max_retries": 3}

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
            # Log traceback to help debugging
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

app = FastAPI(title="NewsAgent Scheduler & Archive", lifespan=lifespan)

# --- 1. WEBHOOK ENDPOINT ---
@app.post("/webhook/store-result")
async def store_result(payload: Dict[str, Any]):
    url = payload.get("source_url")
    data = payload.get("data")

    if not url or not data:
        raise HTTPException(status_code=400, detail="Invalid Payload")

    print(f"[WEBHOOK] 📥 Received result for: {url}")

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
            "source_id": "manual_submission",
            "url": url,
            "status": "processed",
            "discovered_at": datetime.now(timezone.utc),
            "processed_at": datetime.now(timezone.utc),
            "final_output": data
        })

    return {"status": "ok", "message": "Result stored"}

# --- 1.5. LOGS VIEWER ENDPOINT ---
@app.get("/logs", response_class=HTMLResponse)
async def view_logs(
    lines: int = Query(100, ge=10, le=5000, description="Number of log lines to retrieve"),
    grep: Optional[str] = Query(None, description="Filter logs by keyword"),
    since: Optional[str] = Query(None, description="Time duration (e.g., '1h', '30m', '1d')")
):
    """
    View container logs in a formatted HTML page.
    Useful for debugging without SSH access to the VM.
    """
    container_name = "newsagent_scheduler"
    logs = get_container_logs(
        container_name=container_name,
        lines=lines,
        grep_filter=grep,
        since=since
    )
    
    html = format_logs_html(
        logs=logs,
        service_name="Scheduler Service",
        container_name=container_name,
        lines=lines,
        grep_filter=grep
    )
    
    return HTMLResponse(content=html)

# --- 2. SOURCE MANAGEMENT ENDPOINTS ---

@app.post("/sources", status_code=201)
async def add_source(source: SourceConfig):
    source_dict = source.dict(by_alias=True)
    if "created_at" in source_dict and source_dict["created_at"].tzinfo is None:
        source_dict["created_at"] = source_dict["created_at"].replace(tzinfo=timezone.utc)

    try:
        sources_col.insert_one(source_dict)
        return {"status": "created", "id": source_dict["_id"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sources")
async def list_sources():
    return list(sources_col.find())

@app.get("/sources/{source_id}")
async def get_source(source_id: str):
    source = sources_col.find_one({"_id": source_id})
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source

@app.patch("/sources/{source_id}")
async def update_source(source_id: str, updates: Dict[str, Any] = Body(...)):
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
    query = {}
    if status:
        query["status"] = status

    cursor = articles_col.find(query).sort("discovered_at", -1).skip(skip).limit(limit)
    return list(cursor)

@app.get("/articles/{article_id}")
async def get_article(article_id: str):
    article = articles_col.find_one({"_id": article_id})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article

@app.patch("/articles/{article_id}/status")
async def update_article_status(article_id: str, status_update: Dict[str, str] = Body(...)):
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
    # uuid is now imported at top
    uvicorn.run(app, host="0.0.0.0", port=8001)