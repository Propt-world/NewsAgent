import uvicorn
import uuid
import json
import redis
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pprint import pprint

# Project Imports
from src.configs.settings import settings
from src.models.InvokeRequest import InvokeRequest
from src.graph.graph import MainWorkflow
# Note: User moved this file to src/
from src.draw_workflow_graph import generate_workflow_graph

api = FastAPI(
    title="NewsAgent Server",
    version="3.1",
    description="Redis-Backed Async News Agent with Observability"
)

# Initialize Redis Connection Pool
# Creating a global pool is best practice for FastAPI
redis_pool = redis.ConnectionPool.from_url(settings.REDIS_URL)

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
        r = redis.Redis(connection_pool=redis_pool)
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

@api.get("/queue-status")
async def get_queue_status():
    """
    Returns the current count of jobs in the queue.
    """
    try:
        r = redis.Redis(connection_pool=redis_pool)
        count = r.llen(settings.REDIS_QUEUE_NAME)
        return {
            "status": "operational",
            "queue_name": settings.REDIS_QUEUE_NAME,
            "pending_jobs": count
        }
    except Exception as e:
        pprint(f"[API] Error getting queue status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- 2. GRAPH VISUALIZATION ENDPOINT ---
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

# --- 3. JOB SUBMISSION ENDPOINT ---
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
        r = redis.Redis(connection_pool=redis_pool)

        # LPUSH pushes to the left of the list
        r.lpush(settings.REDIS_QUEUE_NAME, json.dumps(job_payload))

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
    print(f"\n--- NewsAgent API v3.1 running on http://{settings.HOST}:{settings.PORT} ---")
    print(f"--- Redis Target: {settings.REDIS_URL} ---")
    uvicorn.run(
        api,
        host=settings.HOST,
        port=settings.PORT,
    )