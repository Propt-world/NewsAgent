import uvicorn
from fastapi import FastAPI
from pprint import pprint
from typing import AsyncGenerator

# 1. Import the MainWorkflow CLASS from your graph/graph.py file
# (Adjust 'src.graph.graph' if you named the file differently)
from src.graph.graph import MainWorkflow
from src.models.MainWorkflowState import MainWorkflowState
from src.configs.settings import settings
from src.models.InvokeRequest import InvokeRequest

# 2. Create the app ---
workflow_builder = MainWorkflow()
app = workflow_builder.create_workflow()
pprint("[SERVER] Graph compiled successfully.")

# Create the FastAPI app ---
api = FastAPI(
    title="NewsAgent Server",
    version="1.0",
    description="API for running the NewsAgent LangGraph workflow",
)

# Create a custom /invoke endpoint ---
@api.post(
    "/invoke",
    response_model=MainWorkflowState,
    summary="Invoke the NewsAgent Workflow (Blocking)"
)
async def invoke_workflow(request: InvokeRequest) -> MainWorkflowState:
    """
    Run the full NewsAgent graph from a simple URL input.
    This is a blocking call that waits for the entire graph to finish.
    """
    pprint(f"[API /invoke] Received request for: {request.source_url}")

    # 1. Create the initial MainWorkflowState from the simple request
    initial_state = MainWorkflowState(
        source_url=request.source_url,
        max_retries=request.max_retries
    )

    result = app.invoke(initial_state)

    pprint(f"[API /invoke] Workflow complete.")
    return result

# Create a custom /stream endpoint ---
@api.post(
    "/stream",
    summary="Stream the NewsAgent Workflow (Async)"
)
async def stream_workflow(request: InvokeRequest) -> AsyncGenerator[MainWorkflowState, None]:
    """
    Run the full NewsAgent graph and stream each node's output.
    This is an async, non-blocking call.
    """
    pprint(f"[API /stream] Received request for: {request.source_url}")

    # 1. Create the initial MainWorkflowState
    initial_state = MainWorkflowState(
        source_url=request.source_url,
        max_retries=request.max_retries
    )

    # 2. Use .astream() to get an async generator
    pprint("[API /stream] Beginning workflow stream...")

    async for event in app.astream(initial_state):
        yield event
    pprint("[API /stream] Workflow stream complete.")

# --- Main entry point to run the server ---
if __name__ == "__main__":

    # Run the FastAPI server ---
    print(f"\n--- Starting FastAPI server on http://{settings.HOST}:{settings.PORT} ---")
    print(f"--- View API Docs at http://{settings.HOST}:{settings.PORT}/docs ---")
    uvicorn.run(
        api,
        host=settings.HOST,
        port=settings.PORT,
    )