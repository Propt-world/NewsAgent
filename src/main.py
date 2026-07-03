import asyncio
import uvicorn
import uuid
import json
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Body, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import List, Optional, Dict, Any
from pprint import pprint
from redis.exceptions import ConnectionError as RedisConnectionError

# Project Imports
from src.configs.settings import settings
from src.models.InvokeRequest import InvokeRequest
from src.db.models import PromptTemplate, Category, EmailRecipient
from src.db.connections import (
    close_async_api_connections,
    close_legacy_api_connections,
    get_async_mongo_database,
    get_async_redis_client,
    get_legacy_mongo_database,
)
from src.graph.graph import MainWorkflow
from src.draw_workflow_graph import generate_workflow_graph
from src.utils.security import verify_api_key  # Import the guard
from src.models.Responses import (
    JobSubmissionResponse,
    JobStatusResponse,
    GenericResponse,
    DLQCountResponse,
    QueueInfo,
    QueueStatusResponse,
    HealthResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await close_async_api_connections()
        close_legacy_api_connections()

api = FastAPI(
    title="NewsAgent Server",
    version="3.4",
    description="Redis-Backed Async News Agent with Observability & Queue Management",
    root_path="/newsapi",
    lifespan=lifespan,
)

# Add CORS middleware
api.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HELPER FUNCTIONS ---
def get_mongo_db():
    return get_legacy_mongo_database()


def decode_job_data(raw_data: bytes | str) -> Dict[str, Any]:
    """Helper to decode bytes from Redis to JSON dict."""
    try:
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8")
        return json.loads(raw_data)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return {"raw_content": str(raw_data)}


def serialize_mongo_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


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
async def health_check(check_external: bool = True):
    """
    Standard Health Check.
    Verifies:
    1. Queue Service (Infrastructure)
    2. Browserless Service (Dependency)
    3. Scheduler Service (Dependency)
    4. Graph Compilation (Code Logic)
    """
    import httpx  # Lazy import to avoid circular dep if any, though top level is fine usually.
    
    health_status = {
        "status": "healthy", 
        "queue": "unknown", 
        "browserless": "unknown",
        "scheduler": "unknown",
        "graph_logic": "unknown"
    }

    # A. Check Queue (Redis)
    try:
        r = get_async_redis_client()
        if await r.ping():
            health_status["queue"] = "connected"
    except Exception as e:
        health_status["queue"] = f"disconnected: {str(e)}"
        health_status["status"] = "unhealthy"

    # B. Check Browserless
    if settings.BROWSERLESS_URL:
        try:
            url = f"{settings.BROWSERLESS_URL}/pressure"
            params = {}
            if settings.BROWSERLESS_TOKEN:
                params["token"] = settings.BROWSERLESS_TOKEN
                
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    health_status["browserless"] = "connected"
                else:
                    health_status["browserless"] = f"degraded ({resp.status_code})"
                    health_status["status"] = "unhealthy" # Critical dependency
        except Exception:
            health_status["browserless"] = "unreachable"
            health_status["status"] = "unhealthy"
    else:
         health_status["browserless"] = "unconfigured"
         health_status["status"] = "unhealthy"


    # C. Check Scheduler
    if check_external:
        try:
            # Recursion Breaker: Pass check_external=false
            scheduler_health_url = f"{settings.SCHEDULER_URL}/health"
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(scheduler_health_url, params={"check_external": "false"})
                if resp.status_code == 200:
                    health_status["scheduler"] = "reachable"
                else:
                     health_status["scheduler"] = f"degraded ({resp.status_code})"
                     health_status["status"] = "unhealthy"
        except Exception:
            health_status["scheduler"] = "unreachable"
            health_status["status"] = "unhealthy"
    else:
        health_status["scheduler"] = "skipped"

    # D. Check Graph Logic
    try:
        # We try to compile the graph. If there's a syntax error or
        # missing node in the definition, this throws an error.
        builder = MainWorkflow()
        builder.create_workflow()
        health_status["graph_logic"] = "operational"
    except Exception as e:
        health_status["graph_logic"] = f"failed: {str(e)}"
        health_status["status"] = "unhealthy"
    
    # Check if we failed because of skipped deps? No, we just report skipped.
    # But if status is "unhealthy" solely due to skipped? 
    # If check_external is False, we shouldn't fail due to "skipped".
    if not check_external and health_status["scheduler"] == "skipped":
         if health_status["status"] == "unhealthy":
             if health_status["queue"] == "connected" and health_status["browserless"] == "connected" and health_status["graph_logic"] == "operational":
                  health_status["status"] = "healthy"

    if health_status["status"] == "unhealthy":
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
        r = get_async_redis_client()
        main_count = await r.llen(settings.REDIS_QUEUE_NAME)
        dlq_count = await r.llen(settings.REDIS_DLQ_NAME)

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
        r = get_async_redis_client()
        # LRANGE is inclusive for start and stop, so we calculate end index carefully
        end_index = offset + limit - 1
        items_raw = await r.lrange(settings.REDIS_QUEUE_NAME, offset, end_index)

        return [decode_job_data(item) for item in items_raw]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.get(
    "/queue/dlq/count",
    status_code=status.HTTP_200_OK,
    response_model=DLQCountResponse,
    description="Returns just the count of items in the Dead Letter Queue.",
    responses={
        status.HTTP_200_OK: {"model": DLQCountResponse, "description": "Count retrieved"},
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
        r = get_async_redis_client()
        count = await r.llen(settings.REDIS_DLQ_NAME)
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
        r = get_async_redis_client()
        end_index = offset + limit - 1
        items_raw = await r.lrange(settings.REDIS_DLQ_NAME, offset, end_index)

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
        r = get_async_redis_client()

        # 1. Fetch all items (up to a reasonable limit, e.g., 1000, to prevent blocking)
        # Ideally, DLQ shouldn't be massive.
        dlq_items = await r.lrange(settings.REDIS_DLQ_NAME, 0, -1)

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
        await r.lrem(settings.REDIS_DLQ_NAME, 1, target_item_raw)

        # 4. Push to Main Queue (Right or Left side? Usually Left/Head for priority, or Right/Tail for fairness)
        # We'll push to the head (Left) so it gets processed next.
        await r.lpush(settings.REDIS_QUEUE_NAME, target_item_raw)

        # 5. Update Status in Hash
        await r.hset(
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
        r = get_async_redis_client()
        count = 0

        # Check initial length
        dlq_len = await r.llen(settings.REDIS_DLQ_NAME)
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
            item = await r.rpoplpush(settings.REDIS_DLQ_NAME, settings.REDIS_QUEUE_NAME)

            if item is None:
                break

            # Optional: Update status for each moved job
            try:
                data = decode_job_data(item)
                if job_id := data.get("job_id"):
                    await r.hset(f"job:{job_id}", mapping={"status": "re-queued"})
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
        r = get_async_redis_client()

        # 1. Fetch all items to find the match (Redis List limitation)
        dlq_items = await r.lrange(settings.REDIS_DLQ_NAME, 0, -1)

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
        removed_count = await r.lrem(settings.REDIS_DLQ_NAME, 1, target_item_raw)

        # 4. Cleanup status (Optional: mark as deleted or expire immediately)
        await r.delete(f"job:{job_id}")

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
        mermaid_syntax, png_path = await asyncio.to_thread(
            generate_workflow_graph,
            xray=True,
            output_dir=output_dir,
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
        r = get_async_redis_client()

        # LPUSH pushes to the left of the list
        await r.lpush(settings.REDIS_QUEUE_NAME, json.dumps(job_payload))

        # --- NEW: SET Initial Status ---
        # We use a hash to store multiple fields (status, url, result)
        # This allows us to track the job lifecycle
        await r.hset(
            f"job:{job_id}",
            mapping={
                "status": "queued",
                "source_url": request.source_url,
                "created_at": str(job_payload["timestamp"]),
            },
        )
        # Set expiry (e.g., 24 hours) so Redis doesn't fill up forever with old status keys
        await r.expire(f"job:{job_id}", 86400)

        pprint(f"[API] Queued Job {job_id} for {request.source_url}")

        # Return Instant Response
        return {
            "job_id": job_id,
            "status": "queued",
            "queue_position": await r.llen(settings.REDIS_QUEUE_NAME),
            "message": "Job successfully sent to Redis worker.",
        }

    except RedisConnectionError:
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
        r = get_async_redis_client()
        # Fetch all fields from the hash "job:{job_id}"
        job_data = await r.hgetall(f"job:{job_id}")

        if not job_data:
            raise HTTPException(
                status_code=404, detail="Job not found (might be expired or invalid ID)"
            )

        # Redis returns bytes, so we must decode them to strings
        decoded_data = {
            k.decode("utf-8"): v.decode("utf-8") for k, v in job_data.items()
        }
        decoded_data["job_id"] = job_id

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
    db = get_async_mongo_database()
    prompts = []
    async for prompt in db["prompts"].find():
        prompts.append(serialize_mongo_doc(prompt))
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
    db = get_async_mongo_database()
    prompt = await db["prompts"].find_one({"_id": prompt_id})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return serialize_mongo_doc(prompt)


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
    db = get_async_mongo_database()
    # Prevent updating immutable fields
    if "_id" in prompt_data:
        del prompt_data["_id"]
    if "created_at" in prompt_data:
        del prompt_data["created_at"]

    result = await db["prompts"].update_one({"_id": prompt_id}, {"$set": prompt_data})
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
    db = get_async_mongo_database()
    cats = []
    async for category in db["categories"].find():
        cats.append(serialize_mongo_doc(category))
    return cats


@api.post(
    "/admin/backfill/countries",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Trigger a backfill job to extract countries for existing articles.",
    responses={
        status.HTTP_200_OK: {"model": GenericResponse, "description": "Backfill completed"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def backfill_countries_endpoint(limit: int = Query(0, description="Max articles to process (0 for all)")):
    """
    Trigger a backfill job to extract countries for existing articles.
    This process runs to completion with async Mongo and LLM calls.
    """
    from src.utils.backfill import run_country_backfill_async
    
    try:
        stats = await run_country_backfill_async(limit=limit)
        return {
            "status": "success",
            "message": f"Backfill complete: Found {stats['found']}, Updated {stats['updated']}, Errors {stats['errors']}",
            # We could return detailed stats if we change the response model, 
            # but GenericResponse usually just has status/message.
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    db = get_async_mongo_database()
    cat_dict = category.dict(by_alias=True)
    # Check duplicate name
    if await db["categories"].find_one({"name": cat_dict["name"]}):
        raise HTTPException(status_code=400, detail="Category already exists")

    await db["categories"].insert_one(cat_dict)
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
    db = get_async_mongo_database()
    if "_id" in updates:
        del updates["_id"]

    result = await db["categories"].update_one({"_id": cat_id}, {"$set": updates})
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
    db = get_async_mongo_database()
    result = await db["categories"].delete_one({"_id": cat_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Category not found")
    return {
        "status": "deleted",
        "message": "Category deleted successfully",
        "id": cat_id,
    }

@api.post(
    "/admin/backfill/social",
    status_code=status.HTTP_200_OK,
    response_model=GenericResponse,
    description="Trigger a backfill job to generate social media captions for existing articles.",
    responses={
        status.HTTP_200_OK: {"model": GenericResponse, "description": "Backfill completed"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": GenericResponse,
            "description": "Internal Server Error",
        },
    },
    dependencies=[Depends(verify_api_key)],
)
async def backfill_social_endpoint(limit: int = Query(0, description="Max articles to process (0 for all)")):
    """
    Trigger a backfill job to generate social media captions.
    Runs to completion with async Mongo and LLM calls.
    """
    from src.utils.backfill_social import run_social_backfill_async

    try:
        stats = await run_social_backfill_async(limit=limit)
        return {
            "status": "success",
            "message": f"Social Backfill complete: Found {stats['found']}, Updated {stats['updated']}, Errors {stats['errors']}",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    db = get_async_mongo_database()
    recipients = []
    async for recipient in db["email_recipients"].find():
        recipients.append(serialize_mongo_doc(recipient))
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
    db = get_async_mongo_database()
    rec_dict = recipient.dict(by_alias=True)

    if await db["email_recipients"].find_one({"email": rec_dict["email"]}):
        raise HTTPException(status_code=400, detail="Email already exists")

    await db["email_recipients"].insert_one(rec_dict)
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
    db = get_async_mongo_database()
    if "_id" in updates:
        del updates["_id"]

    result = await db["email_recipients"].update_one(
        {"_id": rec_id},
        {"$set": updates},
    )
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
    db = get_async_mongo_database()
    result = await db["email_recipients"].delete_one({"_id": rec_id})
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
