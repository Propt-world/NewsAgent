import os

# Settings() requires these to be strings. Tests never call OpenAI or Opik,
# so placeholders are enough; real values from the environment are kept.
for _name in ("OPENAI_API_KEY", "OPENAI_URL", "OPIK_API_KEY", "OPIK_WORKSPACE", "OPIK_PROJECT_NAME"):
    os.environ.setdefault(_name, "test-placeholder")
