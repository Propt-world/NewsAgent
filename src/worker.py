import json
import time
import redis
import traceback
from pprint import pprint

from src.configs.settings import settings
from src.graph.graph import MainWorkflow
from src.models.MainWorkflowState import MainWorkflowState

def run_worker():
    """
    Continuous loop that listens to Redis for new jobs
    and processes them using the LangGraph workflow.
    """
    # 1. Initialize Redis Connection
    try:
        r = redis.from_url(settings.REDIS_URL)
        r.ping() # Check connection
        print(f"--- 👷 NewsAgent Worker Started ---")
        print(f"--- Listening on Queue: {settings.REDIS_QUEUE_NAME} ---")
        print(f"--- Redis URL: {settings.REDIS_URL} ---")
    except Exception as e:
        print(f"[FATAL] Could not connect to Redis: {e}")
        return

    # 2. Build the Graph ONCE (Optimization)
    # We build it here so we don't re-compile the graph for every single job
    workflow_builder = MainWorkflow()
    app_graph = workflow_builder.create_workflow()

    # 3. Main Loop
    while True:
        try:
            # BLPOP blocks the connection until an item is available
            # This is efficient; it doesn't "busy wait" the CPU.
            # Timeout=0 means block forever until a job arrives.
            queue_name, job_data_raw = r.blpop(settings.REDIS_QUEUE_NAME, timeout=0)

            # --- JOB RECEIVED ---
            job_data = json.loads(job_data_raw)
            job_id = job_data.get("job_id")
            source_url = job_data.get("source_url")
            max_retries = job_data.get("max_retries", 3)

            pprint(f"[JOB {job_id}] Processing: {source_url}")

            # 4. Initialize State
            # Note: prompts are loaded automatically by Node 0
            initial_state = MainWorkflowState(
                source_url=source_url,
                max_retries=max_retries
            )

            # 5. Execute Graph
            # The graph handles everything, including the webhook at the end.
            app_graph.invoke(initial_state)

            pprint(f"[JOB {job_id}] Finished successfully.")

        except redis.exceptions.ConnectionError:
            print("[ERROR] Lost connection to Redis. Retrying in 5s...")
            time.sleep(5)
        except Exception as e:
            print(f"[ERROR] Worker crashed on job: {e}")
            traceback.print_exc()
            # In a real system, you might push this job to a "dead-letter queue" here

if __name__ == "__main__":
    run_worker()