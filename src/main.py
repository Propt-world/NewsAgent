import uvicorn
import uuid
import json
import redis
import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from typing import List, Optional, Dict, Any
from pprint import pprint

# Project Imports
from src.configs.settings import settings
from src.models.InvokeRequest import InvokeRequest
from src.graph.graph import MainWorkflow
# Note: User moved this file to src/
from src.draw_workflow_graph import generate_workflow_graph

api = FastAPI(
    title="NewsAgent Server",
    version="3.2",
    description="Redis-Backed Async News Agent with Observability & Queue Management"
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

def decode_job_data(raw_data: bytes) -> Dict[str, Any]:
    """Helper to decode bytes from Redis to JSON dict."""
    try:
        return json.loads(raw_data.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"raw_content": str(raw_data)}

# --- 1. HEALTH CHECK ENDPOINT ---
@api.get("/health", status_code=200)
async def health_check():
    """
    Standard Health Check.
    Verifies:
    1. Redis connectivity (Infrastructure)
    2. Graph Compilation (Code Logic)
    """
    health_status = {
        "status": "healthy",
        "redis": "unknown",
        "graph_logic": "unknown"
    }

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

@api.get("/queue/status")
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
            "main_queue": {
                "name": settings.REDIS_QUEUE_NAME,
                "count": main_count
            },
            "dead_letter_queue": {
                "name": settings.REDIS_DLQ_NAME,
                "count": dlq_count
            }
        }
    except Exception as e:
        pprint(f"[API] Error getting queue status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api.get("/queue/main/items", response_model=List[Dict[str, Any]])
async def list_main_queue_items(
    limit: int = Query(10, ge=1, le=100, description="Number of items to fetch"),
    offset: int = Query(0, ge=0, description="Start index")
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

@api.get("/queue/dlq/count")
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

@api.get("/queue/dlq/items", response_model=List[Dict[str, Any]])
async def list_dlq_items(
    limit: int = Query(10, ge=1, le=100, description="Number of items to fetch"),
    offset: int = Query(0, ge=0, description="Start index")
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

@api.post("/queue/dlq/requeue/{job_id}")
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
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found in DLQ")

        # 3. Remove from DLQ
        # LREM(key, count, value) - count 1 means remove first occurrence
        r.lrem(settings.REDIS_DLQ_NAME, 1, target_item_raw)

        # 4. Push to Main Queue (Right or Left side? Usually Left/Head for priority, or Right/Tail for fairness)
        # We'll push to the head (Left) so it gets processed next.
        r.lpush(settings.REDIS_QUEUE_NAME, target_item_raw)

        # 5. Update Status in Hash
        r.hset(f"job:{job_id}", mapping={"status": "re-queued", "error": ""}) # Clear error

        return {"status": "success", "message": f"Job {job_id} moved from DLQ to Main Queue"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api.post("/queue/dlq/requeue-all")
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
                pass # Ignore decode errors during bulk move

            count += 1

        return {"status": "success", "moved_count": count}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- 3. GRAPH VISUALIZATION ENDPOINT ---
@api.get("/debug/draw-graph", response_class=FileResponse)
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
            xray=True,
            output_dir=output_dir
        )

        if not os.path.exists(png_path):
            raise HTTPException(status_code=500, detail="Graph generation failed (No file created).")

        return FileResponse(png_path, media_type="image/png")

    except Exception as e:
        pprint(f"[API] Graph Draw Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- 4. JOB SUBMISSION ENDPOINT ---
@api.post("/submit-job", status_code=202)
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
        "timestamp": str(uuid.uuid1().time)
    }

    try:
        # Push to Redis
        r = get_redis_client()

        # LPUSH pushes to the left of the list
        r.lpush(settings.REDIS_QUEUE_NAME, json.dumps(job_payload))

        # --- NEW: SET Initial Status ---
        # We use a hash to store multiple fields (status, url, result)
        # This allows us to track the job lifecycle
        r.hset(f"job:{job_id}", mapping={
            "status": "queued",
            "source_url": request.source_url,
            "created_at": str(job_payload["timestamp"])
        })
        # Set expiry (e.g., 24 hours) so Redis doesn't fill up forever with old status keys
        r.expire(f"job:{job_id}", 86400)

        pprint(f"[API] Queued Job {job_id} for {request.source_url}")

        # Return Instant Response
        return {
            "job_id": job_id,
            "status": "queued",
            "queue_position": r.llen(settings.REDIS_QUEUE_NAME),
            "message": "Job successfully sent to Redis worker."
        }

    except redis.exceptions.ConnectionError:
        pprint("[API] CRITICAL: Cannot connect to Redis.")
        raise HTTPException(status_code=503, detail="Queue service unavailable")
    except Exception as e:
        pprint(f"[API] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    print(f"\n--- NewsAgent API v3.2 running on http://{settings.HOST}:{settings.PORT} ---")
    print(f"--- Redis Target: {settings.REDIS_URL} ---")
    uvicorn.run(
        api,
        host=settings.HOST,
        port=settings.PORT,
    )