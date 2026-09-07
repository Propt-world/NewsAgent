import asyncio
import json
import signal
import sys
import traceback
from pprint import pprint

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError

from src.configs.settings import settings
from src.graph.graph import MainWorkflow
from src.graph.nodes.load_agent_configuration import close_async_config_client
from src.models.MainWorkflowState import MainWorkflowState
from src.utils.email_utils import close_async_email_client, send_error_email_async
from src.utils.governance import close_async_governance_clients

# Spot grace period execution budget (seconds)
# Fargate Spot gives 120s from SIGTERM to SIGKILL. We limit workflow execution to 110s
# to leave 10s for DB/Redis status updates, re-queueing, and clean resource teardown.
WORKFLOW_TIMEOUT_SECONDS = 110.0


async def update_job_status(r, job_id, status, result=None, error=None):
    """
    Helper to update Redis Job Status hash key (job:{job_id}).
    Allows external monitoring tools to check processing, completed, or failed.
    """
    try:
        mapping = {"status": status}
        if result:
            mapping["result"] = json.dumps(result)
        if error:
            mapping["error"] = str(error)

        await r.hset(f"job:{job_id}", mapping=mapping)
        pprint(f"[REDIS] Job {job_id} -> {status}")
    except Exception as e:
        print(f"[ERROR] Failed to update Redis status: {e}")


async def run_worker():
    """
    Continuous loop that listens to Redis for new jobs and processes them using
    the LangGraph workflow. Includes resilient SIGTERM/SIGINT signal handling for
    AWS Fargate Spot termination notices with in-flight task protection and re-queueing.
    """
    r = None
    shutdown_requested = asyncio.Event()

    def request_shutdown(signum=None, frame=None):
        print("\n[WORKER] Signal received (SIGTERM/SIGINT). Initiating graceful shutdown...")
        shutdown_requested.set()

    # Register OS signal handlers (works on POSIX/Linux and handles Windows gracefully)
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except NotImplementedError:
                # Windows event loop fallback
                signal.signal(sig, request_shutdown)
    except Exception as sig_err:
        print(f"[WORKER] Signal handler registration warning: {sig_err}")

    try:
        r = redis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.ping()
        print("--- NewsAgent Worker Started (Async) ---")
        print(f"--- Listening on Queue: {settings.REDIS_QUEUE_NAME} ---")
        print(f"--- Redis URL: {settings.REDIS_URL} ---")
    except Exception as e:
        print(f"[FATAL] Could not connect to Redis: {e}")
        return

    try:
        workflow_builder = MainWorkflow()
        app_graph = workflow_builder.create_workflow()

        opik_tracer = None
        if settings.OPIK_API_KEY:
            try:
                print("[WORKER] Initializing Opik tracing...")
                opik_tracer = settings.get_opik_client(
                    graph=app_graph.get_graph(xray=True)
                )
            except Exception as e:
                print(f"[WORKER] Could not initialize Opik: {e}")

        while not shutdown_requested.is_set():
            try:
                # 1. Wait for either a new job or the shutdown signal concurrently
                blpop_task = asyncio.create_task(r.blpop(settings.REDIS_QUEUE_NAME, timeout=10))
                shutdown_task = asyncio.create_task(shutdown_requested.wait())

                done, pending = await asyncio.wait(
                    [blpop_task, shutdown_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

                # If shutdown was signaled while waiting
                if shutdown_requested.is_set():
                    # Check if blpop happened to return a job right before/during shutdown
                    if blpop_task in done:
                        try:
                            blpop_res = blpop_task.result()
                            if blpop_res:
                                _, job_data_raw = blpop_res
                                print("[WORKER] Shutdown active. Re-queueing job back to Redis...")
                                await r.lpush(settings.REDIS_QUEUE_NAME, job_data_raw)
                        except Exception:
                            pass
                    break

                result = blpop_task.result()
                if not result:
                    continue

                queue_name, job_data_raw = result
                job_data = json.loads(job_data_raw)
                job_id = job_data.get("job_id")
                source_url = job_data.get("source_url")
                max_retries = job_data.get("max_retries", 3)

                pprint(f"[JOB {job_id}] Processing: {source_url}")
                await update_job_status(r, job_id, "processing")

                initial_state = MainWorkflowState(
                    source_url=source_url,
                    max_retries=max_retries,
                )

                try:
                    run_config = {}
                    if opik_tracer:
                        run_config["callbacks"] = [opik_tracer]

                    # Execute workflow with timeout to guarantee room for graceful Spot teardown
                    final_state = await asyncio.wait_for(
                        app_graph.ainvoke(
                            initial_state,
                            config=run_config,
                        ),
                        timeout=WORKFLOW_TIMEOUT_SECONDS,
                    )

                    error_message = final_state.get("error_message")

                    if error_message:
                        print(f"[JOB {job_id}] Logic failed: {error_message}")
                        await update_job_status(r, job_id, "failed", error=error_message)

                        job_data["error"] = error_message
                        await r.lpush(settings.REDIS_DLQ_NAME, json.dumps(job_data))

                        await send_error_email_async(
                            job_id=job_id,
                            source_url=source_url,
                            error_details=error_message,
                        )
                    else:
                        pprint(f"[JOB {job_id}] Finished successfully.")
                        article_data = final_state.get("news_article").dict()
                        await update_job_status(
                            r,
                            job_id,
                            "completed",
                            result=article_data,
                        )

                except asyncio.CancelledError:
                    print(f"[JOB {job_id}] Cancelled (Spot termination / shutdown). Re-queueing job unconditionally...")
                    await update_job_status(r, job_id, "queued")
                    await r.lpush(settings.REDIS_QUEUE_NAME, job_data_raw)
                    break

                except asyncio.TimeoutError:
                    attempt = job_data.get("attempt", 0) + 1
                    error_msg = f"Workflow exceeded {WORKFLOW_TIMEOUT_SECONDS}s timeout (attempt {attempt}/{max_retries})"
                    print(f"[JOB {job_id}] {error_msg}")

                    if attempt >= max_retries:
                        print(f"[JOB {job_id}] Max retries reached on timeout. Moving to DLQ.")
                        await update_job_status(r, job_id, "failed", error=error_msg)

                        job_data["attempt"] = attempt
                        job_data["error"] = error_msg
                        await r.lpush(settings.REDIS_DLQ_NAME, json.dumps(job_data))

                        await send_error_email_async(
                            job_id=job_id,
                            source_url=source_url,
                            error_details=error_msg,
                        )
                    else:
                        print(f"[JOB {job_id}] Re-queueing job for retry {attempt}/{max_retries}...")
                        job_data["attempt"] = attempt
                        await update_job_status(r, job_id, "queued")
                        await r.lpush(settings.REDIS_QUEUE_NAME, json.dumps(job_data))

                    if shutdown_requested.is_set():
                        print(f"[JOB {job_id}] Shutdown requested while handling timeout. Exiting loop.")
                        break
                    else:
                        continue

                except Exception as execution_error:
                    error_msg_str = str(execution_error)
                    print(f"[JOB {job_id}] Critical execution error: {error_msg_str}")
                    traceback.print_exc()

                    await update_job_status(r, job_id, "crashed", error=error_msg_str)

                    job_data["error"] = error_msg_str
                    job_data["traceback"] = traceback.format_exc()
                    await r.lpush(settings.REDIS_DLQ_NAME, json.dumps(job_data))

                    await send_error_email_async(
                        job_id=job_id,
                        source_url=source_url,
                        error_details=error_msg_str,
                        traceback_info=traceback.format_exc(),
                    )

            except RedisConnectionError:
                if not shutdown_requested.is_set():
                    print("[ERROR] Lost connection to Redis. Retrying in 5s...")
                    await asyncio.sleep(5)
                else:
                    break
            except Exception as e:
                if not shutdown_requested.is_set():
                    print(f"[ERROR] Worker loop error: {e}")
                    traceback.print_exc()
                    await asyncio.sleep(1)
                else:
                    break
    finally:
        print("[WORKER] Cleaning up resources before shutdown...")
        await close_async_config_client()
        await close_async_email_client()
        await close_async_governance_clients()
        if r is not None:
            await r.aclose()
        print("[WORKER] Worker shutdown complete.")


if __name__ == "__main__":
    asyncio.run(run_worker())

