import uvicorn
from fastapi import FastAPI
from langserve import add_routes
from pprint import pprint

# --- Import the Graph class ---
from src.graph.graph import MainWorkflow
from src.models.MainWorkflowState import MainWorkflowState
from src.configs.settings import settings

# --- Initialize the workflow and compile the app ---
print("[SERVER] Initializing MainWorkflow...")
workflow_builder = MainWorkflow()
app = workflow_builder.create_workflow()
print("[SERVER] Graph compiled successfully.")

# --- Create the FastAPI app ---
api = FastAPI(
    title="NewsAgent Server",
    version="1.0",
    description="API for running the NewsAgent LangGraph workflow",
)

# Add the LangServe routes
add_routes(
    api,
    app,
    path="/invoke",
    input_type=MainWorkflowState,
    output_type=MainWorkflowState,
)

add_routes(
    api,
    app,
    path="/stream",
    input_type=MainWorkflowState,
    output_type=MainWorkflowState,
)

# Main entry point to run the server
if __name__ == "__main__":

    # --- Optional: Test the graph by running python src/main.py ---
    print("--- [TEST] Testing graph execution (this is not the server) ---")

    # --- !!! CHANGE THIS TO A REAL URL FOR TESTING !!! ---
    test_url = "https://www.some-news-site.com/real-article"

    inputs = {"source_url": test_url, "max_retries": 3}

    try:
        result = app.invoke(inputs)
        pprint("--- [TEST] FINAL STATE ---")
        pprint(result)

    except Exception as e:
        print(f"\n[TEST] Graph test failed.")
        print(f"Error: {e}")
        print("Please ensure all API keys are set in your .env file.")

    # --- Run the FastAPI server ---
    print(f"\n--- Starting FastAPI server on http://{settings.HOST}:{settings.PORT} ---")
    uvicorn.run(
        api,
        host=settings.HOST,
        port=settings.PORT,
    )