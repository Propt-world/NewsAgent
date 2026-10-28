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
from src.graph.nodes.check_real_estate import is_skipped
from src.graph.nodes.load_agent_configuration import close_async_config_client
from src.models.MainWorkflowState import MainWorkflowState
from src.utils.email_utils import close_async_email_client, send_error_email_async
from src.utils.governance import close_async_governance_clients
from src.utils import tracing

# Spot grace period execution budget (seconds)
# Fargate Spot gives 120s from SIGTERM to SIGKILL. We limit workflow execution to 110s
# to leave 10s for DB/Redis status updates, re-queueing, and clean resource teardown.
WORKFLOW_TIMEOUT_SECONDS = 110.0


def _trace_article_outcome(span, final_state) -> None:
    """How the article job ended, at the top of its trace (a logic failure is not an exception)."""
    error_message = final_state.get("error_message")
    article = final_state.get("news_article")
    title = getattr(article, "title", None) if article is not None else None
    if error_message:
        tracing.set_output(span, {"outcome": "failed", "error": error_message})
        tracing.mark_failed(span, error_message)
    elif is_skipped(final_state):
        tracing.set_output(span, {"outcome": "skipped: not real estate", "check": final_state.get("real_estate_check")})
    else:
        tracing.set_output(span, {"outcome": "completed", "title": title})


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


COMPOSE_POLL_SECONDS = 5


async def compose_loop(shutdown_requested: asyncio.Event) -> None:
    """
    Newsroom compose jobs: their own queue and listener, so they never wait behind article jobs.
    A job is taken from Redis only when a slot is free (waiting jobs stay in Redis). It follows the
    worker's shutdown signal: after SIGTERM no new compose job is taken and running ones are handed
    back to the front of the queue at once (they take minutes, longer than the Spot notice).
    Uses its own Redis connection so the hand-back never depends on the article loop's teardown.
    """
    from src.newsroom.compose_worker import REQUEUE, run_compose_job
    from src.newsroom.status import heartbeat_loop

    queue = settings.NEWSROOM_COMPOSE_QUEUE
    slots = asyncio.Semaphore(max(1, settings.NEWSROOM_COMPOSE_CONCURRENT))
    running: dict = {}  # task -> raw job
    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    print(f"--- Listening on Queue: {queue} (Newsroom compose, {settings.NEWSROOM_COMPOSE_CONCURRENT} at once) ---")
    # Reports in every 30 s, so the backoffice and CloudWatch notice a missing compose worker.
    heartbeat = asyncio.create_task(heartbeat_loop(r, shutdown_requested))

    def finished(task: asyncio.Task) -> None:
        running.pop(task, None)
        slots.release()

    try:
        while not shutdown_requested.is_set():
            held = False  # a slot this loop holds that no job has taken over yet
            try:
                # Wait for a free slot, but keep watching for the shutdown signal.
                while slots.locked() and not shutdown_requested.is_set():
                    await asyncio.sleep(0.2)
                if shutdown_requested.is_set():
                    break
                await slots.acquire()
                held = True
                # A short wait, never cancelled midway, so a job Redis hands over cannot be lost;
                # the shutdown signal is checked at least every COMPOSE_POLL_SECONDS.
                result = await r.blpop(queue, timeout=COMPOSE_POLL_SECONDS)
                if not result:
                    continue
                _, job_data_raw = result
                if shutdown_requested.is_set():
                    await r.lpush(queue, job_data_raw)  # taken just as the signal arrived: hand it back
                    break
                run_id = json.loads(job_data_raw).get("run_id")
                task = asyncio.create_task(run_compose_job(run_id))
                running[task] = job_data_raw
                task.add_done_callback(finished)
                held = False  # the job releases it when done
            except RedisConnectionError:
                print("[ERROR] Compose: lost connection to Redis. Retrying in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[ERROR] Compose loop error: {e}")
                traceback.print_exc()
                await asyncio.sleep(1)
            finally:
                if held:
                    slots.release()
    finally:
        heartbeat.cancel()
        # Shutdown (or the worker's final cancellation): hand back every compose job still running.
        for task in list(running):
            task.cancel()
        for task, job_data_raw in list(running.items()):
            try:
                outcome = await task
            except BaseException:
                outcome = None
            if outcome == REQUEUE:
                try:
                    await r.lpush(queue, job_data_raw)
                    print(f"[NEWSROOM] Compose job handed back to the queue: {job_data_raw}")
                except Exception as e:
                    print(f"[NEWSROOM] Could not hand back compose job {job_data_raw}: {e}")
        try:
            await r.aclose()
        except Exception:
            pass


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
        tracing.init_tracing()  # Phoenix, beside Opik while both run (off unless PHOENIX_COLLECTOR_ENDPOINT is set)
        workflow_builder = MainWorkflow()
        app_graph = workflow_builder.create_workflow()

        # Newsroom compose jobs run beside article jobs and stop with the same shutdown signal.
        compose_listener = asyncio.create_task(compose_loop(shutdown_requested)) if settings.newsroom_enabled else None

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
                    check_real_estate=bool(job_data.get("check_real_estate", False)),
                )

                try:
                    run_config = {}
                    if opik_tracer:
                        run_config["callbacks"] = [opik_tracer]

                    # One trace per article: every step and AI call nests under it, with the totals at the top
                    with tracing.job_span("article_pipeline", input_value=source_url, tags=["article"],
                                          metadata={"job_id": job_id, "source_url": source_url,
                                                    "attempt": job_data.get("attempt", 0)}) as job_trace:
                        # Execute workflow with timeout to guarantee room for graceful Spot teardown
                        final_state = await asyncio.wait_for(
                            app_graph.ainvoke(
                                initial_state,
                                config=run_config,
                            ),
                            timeout=WORKFLOW_TIMEOUT_SECONDS,
                        )
                        _trace_article_outcome(job_trace, final_state)

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
                    elif is_skipped(final_state):
                        # Not a failure: the article was judged not to be real estate (no email, no dead-letter queue).
                        pprint(f"[JOB {job_id}] Skipped: not real estate.")
                        await update_job_status(r, job_id, "skipped", result=final_state.get("real_estate_check"))
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
        tracing.flush_tracing()  # the last traces, before the container stops
        print("[WORKER] Worker shutdown complete.")


if __name__ == "__main__":
    asyncio.run(run_worker())

