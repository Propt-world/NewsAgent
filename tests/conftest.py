import os

# Settings() requires these to be strings. Tests never call OpenAI or Opik,
# so placeholders are enough; real values from the environment are kept.
for _name in ("OPENAI_API_KEY", "OPENAI_URL", "OPIK_API_KEY", "OPIK_WORKSPACE", "OPIK_PROJECT_NAME"):
    os.environ.setdefault(_name, "test-placeholder")

import pytest


@pytest.fixture(autouse=True)
def no_real_pending_vectors(monkeypatch):
    """No test ever calls OpenAI for a pending article's vector; their own tests switch it on with a stand-in."""
    from src.configs.settings import settings
    monkeypatch.setattr(settings, "NEWSROOM_PENDING_VECTORS_ENABLED", False)
