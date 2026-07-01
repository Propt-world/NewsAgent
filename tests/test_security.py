import pytest

from fastapi import HTTPException

from src.utils import security


@pytest.mark.asyncio
async def test_verify_api_key_allows_when_unconfigured(monkeypatch):
    monkeypatch.setattr(security.settings, "NEWSAGENT_API_KEY", "")
    assert await security.verify_api_key(None) is True


@pytest.mark.asyncio
async def test_verify_api_key_accepts_valid_key(monkeypatch):
    monkeypatch.setattr(security.settings, "NEWSAGENT_API_KEY", "secret-key")
    assert await security.verify_api_key("secret-key") is True


@pytest.mark.asyncio
async def test_verify_api_key_rejects_invalid_key(monkeypatch):
    monkeypatch.setattr(security.settings, "NEWSAGENT_API_KEY", "secret-key")

    with pytest.raises(HTTPException) as exc:
        await security.verify_api_key("wrong-key")

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_webhook_secret_rejects_invalid_value(monkeypatch):
    monkeypatch.setattr(security.settings, "WEBHOOK_SECRET", "hook-secret")

    with pytest.raises(HTTPException) as exc:
        await security.verify_webhook_secret("invalid")

    assert exc.value.status_code == 401