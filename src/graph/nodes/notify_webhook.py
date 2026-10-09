import traceback
from pprint import pprint

import httpx

from src.configs.settings import settings
from src.models.MainWorkflowState import MainWorkflowState


def webhook_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "NewsAgent/1.0",
    }
    if settings.WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = settings.WEBHOOK_SECRET
    return headers


async def post_webhook(payload: dict, label: str) -> bool:
    """POSTs a result to WEBHOOK_URL. Errors are printed, never raised (as before). True if accepted."""
    try:
        pprint(f"[{label}] POSTing data to {settings.WEBHOOK_URL}...")
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                settings.WEBHOOK_URL,
                json=payload,
                headers=webhook_headers(),
            )
        if response.status_code in [200, 201, 202]:
            pprint(f"[{label}] Success! Downstream service accepted data.")
            return True
        pprint(f"[{label}] Warning: Service returned {response.status_code}: {response.text}")
    except httpx.TimeoutException:
        pprint(f"[{label}] Error: Webhook request timed out.")
    except Exception as e:
        pprint(f"[{label}] Error sending webhook: {e}")
        traceback.print_exc()
    return False


async def notify_webhook(state: MainWorkflowState) -> MainWorkflowState:
    """
    Final Node: Sends the processed article to the configured Webhook URL.
    This allows the workflow to offload persistence to another microservice.
    """
    pprint("[NODE: NOTIFY WEBHOOK] Preparing to send data to downstream service...")

    # --- 0. FAIL FAST CHECK ---
    if state.error_message:
        return state

    if not settings.WEBHOOK_URL:
        pprint("[NODE: NOTIFY WEBHOOK] No WEBHOOK_URL configured. Skipping.")
        return state

    try:
        # 1. Prepare the Payload
        # We ensure we have an article to send
        if not state.news_article:
            pprint("[NODE: NOTIFY WEBHOOK] Error: No article content found to send.")
            return state

        article_data = state.news_article.model_dump(mode="json")

        payload = {
            "source_url": state.source_url,
            "status": "success",
            "data": article_data,
        }
        # Newsroom names travel next to the article, not inside it, so the stored article is unchanged.
        if state.newsroom_names is not None:
            payload["newsroom_names"] = state.newsroom_names

        await post_webhook(payload, "NODE: NOTIFY WEBHOOK")

    except Exception as e:
        pprint(f"[NODE: NOTIFY WEBHOOK] Error sending webhook: {e}")
        traceback.print_exc()

    return state


async def report_not_real_estate(state: MainWorkflowState) -> MainWorkflowState:
    """Final node for articles the real-estate check skipped: tells the scheduler, which marks the
    record "not_real_estate" (so it is never processed again and shows in the Filtered out list)."""
    if not settings.WEBHOOK_URL:
        pprint("[NODE: REPORT NOT REAL ESTATE] No WEBHOOK_URL configured. Skipping.")
        return state
    await post_webhook({
        "source_url": state.source_url,
        "status": "not_real_estate",
        "title": state.news_article.title if state.news_article else None,
        "real_estate_check": state.real_estate_check,
    }, "NODE: REPORT NOT REAL ESTATE")
    return state
