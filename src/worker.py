import asyncio
import json
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
    the LangGraph workflow.
    """
    r = None

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

        while True:
            try:
                result = await r.blpop(settings.REDIS_QUEUE_NAME, timeout=0)
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

                    final_state = await app_graph.ainvoke(
                        initial_state,
                        config=run_config,
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
                print("[ERROR] Lost connection to Redis. Retrying in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[ERROR] Worker loop error: {e}")
                traceback.print_exc()
                await asyncio.sleep(1)
    finally:
        await close_async_config_client()
        await close_async_email_client()
        await close_async_governance_clients()
        if r is not None:
            await r.aclose()


if __name__ == "__main__":
    asyncio.run(run_worker())
