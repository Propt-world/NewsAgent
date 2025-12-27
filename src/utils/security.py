from fastapi import Header, HTTPException, Depends
from typing import Optional
from src.configs.settings import settings

async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """
    FastAPI Dependency: Verifies the 'X-API-Key' header.
    This creates a "Gatekeeper" that rejects requests without the valid token.
    """
    # Allow completely open access ONLY if no key is set in env (Dev mode)
    # In Production, NEWSAGENT_API_KEY must be set.
    if not settings.NEWSAGENT_API_KEY:
        return True

    if x_api_key != settings.NEWSAGENT_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-API-Key header"
        )
    return True

async def verify_webhook_secret(x_webhook_secret: Optional[str] = Header(None)):
    """
    Specific dependency for the Scheduler Webhook.
    """
    if not settings.WEBHOOK_SECRET:
        return True # Or raise error if you want to enforce strictness

    if x_webhook_secret != settings.WEBHOOK_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Invalid X-Webhook-Secret"
        )
    return True