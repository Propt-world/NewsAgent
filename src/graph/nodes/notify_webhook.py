import requests
import traceback
import json
from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState
from src.configs.settings import settings

def notify_webhook(state: MainWorkflowState) -> MainWorkflowState:
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
            # You might want to send an error payload to the webhook instead
            return state

        # Serialize the ArticleModel to JSON
        article_data = state.news_article.model_dump(mode='json')

        # Wrap it with metadata (e.g., source URL, status)
        payload = {
            "source_url": state.source_url,
            "status": "success",
            "data": article_data
        }

        # 2. Prepare Headers
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "NewsAgent/1.0"
        }
        # Add secret if configured (for security on receiver end)
        if settings.WEBHOOK_SECRET:
            headers["X-Webhook-Secret"] = settings.WEBHOOK_SECRET

        # 3. Send Request (with timeout)
        pprint(f"[NODE: NOTIFY WEBHOOK] POSTing data to {settings.WEBHOOK_URL}...")

        response = requests.post(
            settings.WEBHOOK_URL,
            json=payload,
            headers=headers,
            timeout=15 # Give the receiver 15s to acknowledge
        )

        # 4. Check Response
        if response.status_code in [200, 201, 202]:
            pprint(f"[NODE: NOTIFY WEBHOOK] Success! Downstream service accepted data.")
        else:
            pprint(f"[NODE: NOTIFY WEBHOOK] Warning: Service returned {response.status_code}: {response.text}")

    except requests.exceptions.Timeout:
        pprint("[NODE: NOTIFY WEBHOOK] Error: Webhook request timed out.")
    except Exception as e:
        pprint(f"[NODE: NOTIFY WEBHOOK] Error sending webhook: {e}")
        traceback.print_exc()

    return state