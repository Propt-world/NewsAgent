import json
import time
import redis
import traceback
from pprint import pprint

from src.configs.settings import settings
from src.graph.graph import MainWorkflow
from src.models.MainWorkflowState import MainWorkflowState
from src.utils.email_utils import send_error_email

def update_job_status(r, job_id, status, result=None, error=None):
    """
    Helper to update Redis Job Status hash key (job:{job_id}).
    Allows external monitoring tools to check 'processing', 'completed', or 'failed'.
    """
    try:
        mapping = {"status": status}
        if result:
            mapping["result"] = json.dumps(result) # Store result as string for simple retrieval
        if error:
            mapping["error"] = str(error)

        r.hset(f"job:{job_id}", mapping=mapping)
        pprint(f"[REDIS] Job {job_id} -> {status}")
    except Exception as e:
        print(f"[ERROR] Failed to update Redis status: {e}")

def run_worker():
    """
    Continuous loop that listens to Redis for new jobs
    and processes them using the LangGraph workflow.
    """
    # 1. Initialize Redis Connection
    try:
        r = redis.from_url(settings.REDIS_URL)
        r.ping() # Check connection
        print(f"--- 👷 NewsAgent Worker Started ---", flush=True)
        print(f"--- Listening on Queue: {settings.REDIS_QUEUE_NAME} ---", flush=True)
        print(f"--- Redis URL: {settings.REDIS_URL} ---", flush=True)
    except Exception as e:
        print(f"[FATAL] Could not connect to Redis: {e}", flush=True)
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

            # --- NEW: Update Status to Processing ---
            update_job_status(r, job_id, "processing")

            # 4. Initialize State
            # Note: prompts are loaded automatically by Node 0
            initial_state = MainWorkflowState(
                source_url=source_url,
                max_retries=max_retries
            )

            # 5. Execute Graph with Error Handling
            try:
                # The invoke method returns the final state (usually a dict)
                # The graph handles everything, including the webhook at the end.
                final_state = app_graph.invoke(initial_state)

                # Check for logical errors captured within the graph nodes
                # (e.g., newspaper4k failed, OpenAI rate limit, etc.)
                error_message = final_state.get("error_message")

                if error_message:
                    # --- LOGICAL FAILURE ---
                    print(f"[JOB {job_id}] ❌ Logic Failed: {error_message}")

                    # Update Redis Status
                    update_job_status(r, job_id, "failed", error=error_message)

                    # Push to Dead Letter Queue (DLQ) for reprocessing later
                    job_data["error"] = error_message
                    r.lpush(settings.REDIS_DLQ_NAME, json.dumps(job_data))

                    # Send Email
                    send_error_email(
                        job_id=job_id,
                        source_url=source_url,
                        error_details=error_message
                    )
                else:
                    # --- SUCCESS ---
                    pprint(f"[JOB {job_id}] ✅ Finished successfully.")

                    # Store result in Redis status (optional, but good for debugging)
                    article_data = final_state.get("news_article").dict()
                    update_job_status(r, job_id, "completed", result=article_data)

            except Exception as execution_error:
                # --- CRASH FAILURE ---
                # Catch critical crashes (e.g., code bugs, memory errors, graph config errors)
                error_msg_str = str(execution_error)
                print(f"[JOB {job_id}] 💥 CRITICAL EXECUTION ERROR: {error_msg_str}")
                traceback.print_exc()

                # Update Redis Status
                update_job_status(r, job_id, "crashed", error=error_msg_str)

                # Push to Dead Letter Queue (DLQ)
                job_data["error"] = error_msg_str
                job_data["traceback"] = traceback.format_exc()
                r.lpush(settings.REDIS_DLQ_NAME, json.dumps(job_data))

                # Send Email with Traceback
                send_error_email(
                    job_id=job_id,
                    source_url=source_url,
                    error_details=error_msg_str,
                    traceback_info=traceback.format_exc()
                )

        except redis.exceptions.ConnectionError:
            print("[ERROR] Lost connection to Redis. Retrying in 5s...")
            time.sleep(5)
        except Exception as e:
            # Catch-all for Redis extraction or JSON parsing errors
            print(f"[ERROR] Worker loop error: {e}")
            traceback.print_exc()

if __name__ == "__main__":
    try:
        run_worker()
    except KeyboardInterrupt:
        print("Worker stopped by user.", flush=True)
    except Exception as e:
        print(f"CRITICAL WORKER CRASH: {e}", flush=True)
        traceback.print_exc()
    finally:
        print("Worker Exiting...", flush=True)