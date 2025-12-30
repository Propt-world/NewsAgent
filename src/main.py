import uvicorn
import uuid
import json
import redis
import os
from fastapi import FastAPI, HTTPException, Query, Body, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import List, Optional, Dict, Any
from pprint import pprint
from pymongo import MongoClient

# Project Imports
from src.configs.settings import settings
from src.models.InvokeRequest import InvokeRequest
from src.db.models import PromptTemplate, Category, EmailRecipient
from src.graph.graph import MainWorkflow
from src.draw_workflow_graph import generate_workflow_graph
from src.utils.security import verify_api_key  # Import the guard
from src.models.Responses import (
    JobSubmissionResponse,
    JobStatusResponse,
    GenericResponse,
    QueueInfo,
    QueueStatusResponse,
    HealthResponse,
)

api = FastAPI(
    title="NewsAgent Server",
    version="3.4",
    description="Redis-Backed Async News Agent with Observability & Queue Management",
    root_path="/newsapi",
)

# Add CORS middleware
api.add_middleware(
    CORSMiddleware,
    allow_origins=["https://main.d211u21suwdysn.amplifyapp.com", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Redis Connection Pool
# Creating a global pool is best practice for FastAPI
redis_pool = redis.ConnectionPool.from_url(settings.REDIS_URL)

# --- HELPER FUNCTIONS ---
def get_redis_client():
    try:
        return redis.Redis(connection_pool=redis_pool)
    except redis.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="Redis service unavailable")


def get_mongo_db():
    client = MongoClient(settings.DATABASE_URL)
    return client[settings.MONGO_DB_NAME]


def decode_job_data(raw_data: bytes) -> Dict[str, Any]:
    """Helper to decode bytes from Redis to JSON dict."""
    try:
        return json.loads(raw_data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"raw_content": str(raw_data)}


# --- 1. HEALTH CHECK ENDPOINT ---
@api.get(
    "/health",
    status_code=200,
    response_model=HealthResponse,
    description="Standard Health Check.",
    responses={
        status.HTTP_200_OK: {
            "model": HealthResponse,
            "description": "Health check passed",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": HealthResponse,
            "description": "Internal Server Error",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthResponse,
            "description": "Service Unavailable",
        },
    },
)
async def health_check():
    """
    Standard Health Check.
    Verifies:
    1. Redis connectivity (Infrastructure)
    2. Graph Compilation (Code Logic)
    """
    health_status = {"status": "healthy", "redis": "unknown", "graph_logic": "unknown"}

    # A. Check Redis
    try:
        r = get_redis_client()
        if r.ping():
            health_status["redis"] = "connected"
    except Exception as e:
        health_status["redis"] = f"disconnected: {str(e)}"
        health_status["status"] = "unhealthy"
        # If critical infra is down, return 503
        raise HTTPException(status_code=503, detail=health_status)

    # B. Check Graph Logic
    try:
        # We try to compile the graph. If there's a syntax error or
        # missing node in the definition, this throws an error.
        builder = MainWorkflow()
        builder.create_workflow()
        health_status["graph_logic"] = "operational"
    except Exception as e:
        health_status["graph_logic"] = f"failed: {str(e)}"
        health_status["status"] = "unhealthy"
        raise HTTPException(status_code=503, detail=health_status)

    return health_status


# --- 2. QUEUE MANAGEMENT ENDPOINTS ---


@api.get(
    "/queue/status",
    status_code=status.HTTP_200_OK,
    response_model=QueueStatusResponse,
    description="Returns current counts for Main and Dead Letter queues.",
    responses={
        status.HTTP_200_OK: {
            "model": QueueStatusResponse,
            "description": "Queue metrics retrieved",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def get_queue_status():
    """
    Returns the current count of jobs in both the Main Queue and Dead Letter Queue.
    """
    try:
        r = get_redis_client()
        main_count = r.llen(settings.REDIS_QUEUE_NAME)
        dlq_count = r.llen(settings.REDIS_DLQ_NAME)

        return {
            "status": "operational",
            "main_queue": {"name": settings.REDIS_QUEUE_NAME, "count": main_count},
            "dead_letter_queue": {"name": settings.REDIS_DLQ_NAME, "count": dlq_count},
        }
    except Exception as e:
        pprint(f"[API] Error getting queue status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api.get(
    "/queue/main/items",
    status_code=status.HTTP_200_OK,
    response_model=List[Dict[str, Any]],
    description="Returns a list of items in the Main Queue.",
    responses={
        status.HTTP_200_OK: {
            "model": List[Dict[str, Any]],
            "description": "Items retrieved",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def list_main_queue_items(
    limit: int = Query(10, ge=1, le=100, description="Number of items to fetch"),
    offset: int = Query(0, ge=0, description="Start index"),
):
    """
    Fetches a detailed list of items currently in the Main Queue.
    Includes pagination to avoid fetching massive lists.
    """
    try:
        r = get_redis_client()
        # LRANGE is inclusive for start and stop, so we calculate end index carefully
        end_index = offset + limit - 1
        items_raw = r.lrange(settings.REDIS_QUEUE_NAME, offset, end_index)

        return [decode_job_data(item) for item in items_raw]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.get(
    "/queue/dlq/count",
    status_code=status.HTTP_200_OK,
    response_model=int,
    description="Returns just the count of items in the Dead Letter Queue.",
    responses={
        status.HTTP_200_OK: {"model": int, "description": "Count retrieved"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def count_dlq_items():
    """
    Returns just the count of items in the Dead Letter Queue.
    """
    try:
        r = get_redis_client()
        count = r.llen(settings.REDIS_DLQ_NAME)
        return {"dlq_count": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.get(
    "/queue/dlq/items",
    status_code=status.HTTP_200_OK,
    response_model=List[Dict[str, Any]],
    description="Returns a list of items in the Dead Letter Queue.",
    responses={
        status.HTTP_200_OK: {
            "model": List[Dict[str, Any]],
            "description": "Items retrieved",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def list_dlq_items(
    limit: int = Query(10, ge=1, le=100, description="Number of items to fetch"),
    offset: int = Query(0, ge=0, description="Start index"),
):
    """
    Fetches a detailed list of items currently in the Dead Letter Queue.
    Includes pagination.
    """
    try:
        r = get_redis_client()
        end_index = offset + limit - 1
        items_raw = r.lrange(settings.REDIS_DLQ_NAME, offset, end_index)

        return [decode_job_data(item) for item in items_raw]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post(
    "/queue/dlq/requeue/{job_id}",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Moves a SPECIFIC item from the Dead Letter Queue back to the Main Queue based on Job ID.",
    responses={
        status.HTTP_200_OK: {"model": GenericResponse, "description": "Item requeued"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def requeue_dlq_item(job_id: str):
    """
    Moves a SPECIFIC item from the Dead Letter Queue back to the Main Queue based on Job ID.
    Note: This is an O(N) operation as it has to search the list.
    """
    try:
        r = get_redis_client()

        # 1. Fetch all items (up to a reasonable limit, e.g., 1000, to prevent blocking)
        # Ideally, DLQ shouldn't be massive.
        dlq_items = r.lrange(settings.REDIS_DLQ_NAME, 0, -1)

        target_item_raw = None
        target_item_json = None

        # 2. Search for the job
        for item in dlq_items:
            data = decode_job_data(item)
            if data.get("job_id") == job_id:
                target_item_raw = item
                target_item_json = data
                break

        if not target_item_raw:
            raise HTTPException(
                status_code=404, detail=f"Job {job_id} not found in DLQ"
            )

        # 3. Remove from DLQ
        # LREM(key, count, value) - count 1 means remove first occurrence
        r.lrem(settings.REDIS_DLQ_NAME, 1, target_item_raw)

        # 4. Push to Main Queue (Right or Left side? Usually Left/Head for priority, or Right/Tail for fairness)
        # We'll push to the head (Left) so it gets processed next.
        r.lpush(settings.REDIS_QUEUE_NAME, target_item_raw)

        # 5. Update Status in Hash
        r.hset(
            f"job:{job_id}", mapping={"status": "re-queued", "error": ""}
        )  # Clear error

        return {
            "status": "success",
            "message": f"Job {job_id} moved from DLQ to Main Queue",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post(
    "/queue/dlq/requeue-all",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Moves ALL items from the Dead Letter Queue back to the Main Queue.",
    responses={
        status.HTTP_200_OK: {"model": GenericResponse, "description": "Items requeued"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def requeue_all_dlq_items():
    """
    Moves ALL items from the Dead Letter Queue back to the Main Queue.
    Using RPOPLPUSH (or LMOVE) in a loop.
    """
    try:
        r = get_redis_client()
        count = 0

        # Check initial length
        dlq_len = r.llen(settings.REDIS_DLQ_NAME)
        if dlq_len == 0:
            return {"status": "success", "message": "DLQ is empty, nothing to move."}

        # Loop and move
        # We use 'RPOPLPUSH' source destination -> Pops tail of source, pushes to head of dest
        # Since we usually LPOP from DLQ to read, items are added via LPUSH.
        # So the oldest items are at the TAIL (Right).
        # RPOPLPUSH is safe and atomic.
        while True:
            # Redis < 6.2 uses RPOPLPUSH, 6.2+ uses LMOVE. RPOPLPUSH is safer for compatibility.
            # Moves element from 'Right' of DLQ to 'Left' of Main Queue.
            item = r.rpoplpush(settings.REDIS_DLQ_NAME, settings.REDIS_QUEUE_NAME)

            if item is None:
                break

            # Optional: Update status for each moved job
            try:
                data = decode_job_data(item)
                if job_id := data.get("job_id"):
                    r.hset(f"job:{job_id}", mapping={"status": "re-queued"})
            except:
                pass  # Ignore decode errors during bulk move

            count += 1

        return {
            "status": "success",
            "message": f"Successfully moved {count} jobs from DLQ to Main Queue",
            "moved_count": count,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.delete(
    "/queue/dlq/{job_id}",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="HARD DELETE: Permanently removes a specific item from the Dead Letter Queue.",
    responses={
        status.HTTP_200_OK: {"model": GenericResponse, "description": "Item deleted"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def delete_dlq_item(job_id: str):
    """
    HARD DELETE: Permanently removes a specific item from the Dead Letter Queue.
    This action cannot be undone.
    """
    try:
        r = get_redis_client()

        # 1. Fetch all items to find the match (Redis List limitation)
        dlq_items = r.lrange(settings.REDIS_DLQ_NAME, 0, -1)

        target_item_raw = None

        # 2. Search for the job
        for item in dlq_items:
            data = decode_job_data(item)
            if data.get("job_id") == job_id:
                target_item_raw = item
                break

        if not target_item_raw:
            raise HTTPException(
                status_code=404, detail=f"Job {job_id} not found in DLQ"
            )

        # 3. Remove the item
        # count=1 means remove the first occurrence of this specific value
        removed_count = r.lrem(settings.REDIS_DLQ_NAME, 1, target_item_raw)

        # 4. Cleanup status (Optional: mark as deleted or expire immediately)
        r.delete(f"job:{job_id}")

        return {
            "status": "success",
            "message": f"Job {job_id} permanently deleted from DLQ",
            "count": removed_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- 3. GRAPH VISUALIZATION ENDPOINT ---
@api.get(
    "/debug/draw-graph",
    status_code=status.HTTP_200_OK,
    description="Generates and returns the current workflow graph visualization (PNG).",
    responses={
        status.HTTP_200_OK: {"description": "Graph image retrieved"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def draw_graph():
    """
    Generates and returns the current workflow graph visualization (PNG).
    Useful for debugging to ensure the graph topology is what you expect.
    """
    try:
        # Define where to save the temp file
        output_dir = "graphs"

        # Call your utility function
        mermaid_syntax, png_path = generate_workflow_graph(
            xray=True, output_dir=output_dir
        )

        if not os.path.exists(png_path):
            raise HTTPException(
                status_code=500, detail="Graph generation failed (No file created)."
            )

        return FileResponse(png_path, media_type="image/png")

    except Exception as e:
        pprint(f"[API] Graph Draw Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- 4. JOB SUBMISSION ENDPOINT ---
@api.post(
    "/submit-job",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobSubmissionResponse,
    description="Accepts a URL, serializes the request, and pushes it to the Redis Queue.",
    responses={
        # Keys are status codes (int), Values are dicts
        status.HTTP_202_ACCEPTED: {
            "model": JobSubmissionResponse,
            "description": "Job accepted for processing",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": GenericResponse,
            "description": "Queue Service Unavailable",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def submit_job(request: InvokeRequest):
    """
    Accepts a URL, serializes the request, and pushes it to the Redis Queue.
    Returns a Job ID immediately.
    """
    job_id = str(uuid.uuid4())

    # Create the Payload
    job_payload = {
        "job_id": job_id,
        "source_url": request.source_url,
        "max_retries": request.max_retries,
        # Optional metadata useful for the worker
        "timestamp": str(uuid.uuid1().time),
    }

    try:
        # Push to Redis
        r = get_redis_client()

        # LPUSH pushes to the left of the list
        r.lpush(settings.REDIS_QUEUE_NAME, json.dumps(job_payload))

        # --- NEW: SET Initial Status ---
        # We use a hash to store multiple fields (status, url, result)
        # This allows us to track the job lifecycle
        r.hset(
            f"job:{job_id}",
            mapping={
                "status": "queued",
                "source_url": request.source_url,
                "created_at": str(job_payload["timestamp"]),
            },
        )
        # Set expiry (e.g., 24 hours) so Redis doesn't fill up forever with old status keys
        r.expire(f"job:{job_id}", 86400)

        pprint(f"[API] Queued Job {job_id} for {request.source_url}")

        # Return Instant Response
        return {
            "job_id": job_id,
            "status": "queued",
            "queue_position": r.llen(settings.REDIS_QUEUE_NAME),
            "message": "Job successfully sent to Redis worker.",
        }

    except redis.exceptions.ConnectionError:
        pprint("[API] CRITICAL: Cannot connect to Redis.")
        raise HTTPException(status_code=503, detail="Queue service unavailable")
    except Exception as e:
        pprint(f"[API] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- 5. JOB STATUS ENDPOINT ---
@api.get(
    "/jobs/{job_id}",
    status_code=status.HTTP_200_OK,
    response_model=JobStatusResponse,
    description="Fetch the real-time status of a specific job from Redis.",
    responses={
        status.HTTP_200_OK: {
            "model": JobStatusResponse,
            "description": "Job status fetched successfully",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GenericResponse,
            "description": "Job not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def get_job_status(job_id: str):
    """
    Fetch the real-time status of a specific job from Redis.
    The worker updates this hash key as it processes the graph.
    """
    try:
        r = get_redis_client()
        # Fetch all fields from the hash "job:{job_id}"
        job_data = r.hgetall(f"job:{job_id}")

        if not job_data:
            raise HTTPException(
                status_code=404, detail="Job not found (might be expired or invalid ID)"
            )

        # Redis returns bytes, so we must decode them to strings
        decoded_data = {
            k.decode("utf-8"): v.decode("utf-8") for k, v in job_data.items()
        }

        # If there is a "result" field (JSON string), parse it back to an object for cleaner API output
        if "result" in decoded_data:
            try:
                decoded_data["result"] = json.loads(decoded_data["result"])
            except:
                pass  # Keep as string if parse fails

        return decoded_data

    except HTTPException:
        raise
    except Exception as e:
        pprint(f"[API] Error fetching job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# 5. ADMINISTRATION ENDPOINTS
# ==========================================

# --- A. PROMPTS ---


@api.get(
    "/admin/prompts",
    status_code=status.HTTP_200_OK,
    response_model=List[Dict],
    description="List all available prompts.",
    responses={
        status.HTTP_200_OK: {"model": List[Dict], "description": "Prompts retrieved"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def list_prompts():
    """List all available prompts."""
    db = get_mongo_db()
    prompts = list(db["prompts"].find())
    # Convert _id to string for JSON serialization
    for p in prompts:
        p["_id"] = str(p["_id"])
    return prompts


@api.get(
    "/admin/prompts/{prompt_id}",
    status_code=status.HTTP_200_OK,
    response_model=Dict,
    description="Get a specific prompt by ID.",
    responses={
        status.HTTP_200_OK: {"model": Dict, "description": "Prompt retrieved"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def get_prompt(prompt_id: str):
    """Get a specific prompt by ID."""
    db = get_mongo_db()
    prompt = db["prompts"].find_one({"_id": prompt_id})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    prompt["_id"] = str(prompt["_id"])
    return prompt


@api.put(
    "/admin/prompts/{prompt_id}",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Update a prompt's content or status.",
    responses={
        status.HTTP_200_OK: {"model": GenericResponse, "description": "Prompt updated"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def update_prompt(prompt_id: str, prompt_data: Dict[str, Any] = Body(...)):
    """Update a prompt's content or status."""
    db = get_mongo_db()
    # Prevent updating immutable fields
    if "_id" in prompt_data:
        del prompt_data["_id"]
    if "created_at" in prompt_data:
        del prompt_data["created_at"]

    result = db["prompts"].update_one({"_id": prompt_id}, {"$set": prompt_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return {
        "status": "updated",
        "message": "Prompt updated successfully",
        "id": prompt_id,
    }


# --- B. CATEGORIES ---


@api.get(
    "/admin/categories",
    status_code=status.HTTP_200_OK,
    response_model=List[Dict],
    description="List all article categories.",
    responses={
        status.HTTP_200_OK: {
            "model": List[Dict],
            "description": "Categories retrieved",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def list_categories():
    """List all article categories."""
    db = get_mongo_db()
    cats = list(db["categories"].find())
    for c in cats:
        c["_id"] = str(c["_id"])
    return cats


@api.post(
    "/admin/categories",
    status_code=status.HTTP_201_CREATED,
    response_model=GenericResponse,
    description="Add a new category.",
    responses={
        status.HTTP_201_CREATED: {
            "model": GenericResponse,
            "description": "Category created",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def add_category(category: Category):
    """Add a new category."""
    db = get_mongo_db()
    cat_dict = category.dict(by_alias=True)
    # Check duplicate name
    if db["categories"].find_one({"name": cat_dict["name"]}):
        raise HTTPException(status_code=400, detail="Category already exists")

    db["categories"].insert_one(cat_dict)
    return {
        "status": "created",
        "message": "Category created successfully",
        "id": cat_dict["_id"],
    }


@api.put(
    "/admin/categories/{cat_id}",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Update a category (e.g. add sub-categories).",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Category updated",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def update_category(cat_id: str, updates: Dict[str, Any] = Body(...)):
    """Update a category (e.g. add sub-categories)."""
    db = get_mongo_db()
    if "_id" in updates:
        del updates["_id"]

    result = db["categories"].update_one({"_id": cat_id}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Category not found")
    return {
        "status": "updated",
        "message": "Category updated successfully",
        "id": cat_id,
    }


@api.delete(
    "/admin/categories/{cat_id}",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Delete a category.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Category deleted",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def delete_category(cat_id: str):
    """Delete a category."""
    db = get_mongo_db()
    result = db["categories"].delete_one({"_id": cat_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Category not found")
    return {
        "status": "deleted",
        "message": "Category deleted successfully",
        "id": cat_id,
    }


# --- C. EMAIL RECIPIENTS ---


@api.get(
    "/admin/email-recipients",
    status_code=status.HTTP_200_OK,
    response_model=List[Dict],
    description="List all email recipients for error alerts.",
    responses={
        status.HTTP_200_OK: {
            "model": List[Dict],
            "description": "Email recipients retrieved",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def list_email_recipients():
    """List all email recipients for error alerts."""
    db = get_mongo_db()
    recipients = list(db["email_recipients"].find())
    for r in recipients:
        r["_id"] = str(r["_id"])
    return recipients


@api.post(
    "/admin/email-recipients",
    status_code=status.HTTP_201_CREATED,
    response_model=GenericResponse,
    description="Add a new email recipient.",
    responses={
        status.HTTP_201_CREATED: {
            "model": GenericResponse,
            "description": "Email recipient created",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def add_email_recipient(recipient: EmailRecipient):
    """Add a new email recipient."""
    db = get_mongo_db()
    rec_dict = recipient.dict(by_alias=True)

    if db["email_recipients"].find_one({"email": rec_dict["email"]}):
        raise HTTPException(status_code=400, detail="Email already exists")

    db["email_recipients"].insert_one(rec_dict)
    return {
        "status": "created",
        "message": "Email recipient added successfully",
        "id": rec_dict["_id"],
    }


@api.put(
    "/admin/email-recipients/{rec_id}",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Update an email recipient (e.g. deactivate).",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Email recipient updated",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def update_email_recipient(rec_id: str, updates: Dict[str, Any] = Body(...)):
    """Update an email recipient (e.g. deactivate)."""
    db = get_mongo_db()
    if "_id" in updates:
        del updates["_id"]

    result = db["email_recipients"].update_one({"_id": rec_id}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Recipient not found")
    return {
        "status": "updated",
        "message": "Email recipient updated successfully",
        "id": rec_id,
    }


@api.delete(
    "/admin/email-recipients/{rec_id}",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Delete an email recipient.",
    responses={
        status.HTTP_200_OK: {
            "model": GenericResponse,
            "description": "Email recipient deleted",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def delete_email_recipient(rec_id: str):
    """Delete an email recipient."""
    db = get_mongo_db()
    result = db["email_recipients"].delete_one({"_id": rec_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Recipient not found")
    return {
        "status": "deleted",
        "message": "Email recipient deleted successfully",
        "id": rec_id,
    }


if __name__ == "__main__":
    print(
        f"\n--- NewsAgent API v3.2 running on http://{settings.HOST}:{settings.PORT} ---"
    )
    print(f"--- Redis Target: {settings.REDIS_URL} ---")
    uvicorn.run(
        api,
        host=settings.HOST,
        port=settings.PORT,
    )
