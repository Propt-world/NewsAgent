import os


def pytest_configure() -> None:
    """Provide safe defaults so settings can initialize in CI/local tests."""
    defaults = {
        "OPENAI_API_KEY": "test-openai-key",
        "OPIK_API_KEY": "test-opik-key",
        "TAVILY_API_KEY": "test-tavily-key",
        "OPENAI_URL": "https://api.openai.com/v1",
        "OPIK_WORKSPACE": "test-workspace",
        "OPIK_PROJECT_NAME": "test-project",
        "MODEL_NAME": "gpt-4o-mini",
        "MODEL_TEMPERATURE": "0.2",
        "REDIS_URL": "redis://localhost:6379/0",
        "REDIS_QUEUE_NAME": "newsagent_jobs",
        "REDIS_DLQ_NAME": "newsagent_dlq",
        "DATABASE_URL": "mongodb://localhost:27017",
        "MONGO_DB_NAME": "newsagent",
        "MONGO_DB_COLLECTION": "vectorized_articles",
        "MONGODB_BIO_COLLECTION": "chatbot_bio",
        "VECTOR_INDEX_NAME": "newsagent_vector_index",
        "AWS_REGION": "us-east-1",
        "BROWSERLESS_URL": "http://localhost:3000",
        "BROWSERLESS_TOKEN": "test-token",
        "BROWSER_WS_ENDPOINT": "ws://localhost:3000",
        "SMTP_SERVER": "smtp.gmail.com",
        "SMTP_PORT": "587",
        "SMTP_EMAIL": "",
        "SMTP_PASSWORD": "",
        "MAIN_API_URL": "http://localhost:8003",
        "SCHEDULER_URL": "http://localhost:8001",
        "NEWSAGENT_API_KEY": "",
        "WEBHOOK_URL": "http://localhost:8001/webhook/store-result",
        "WEBHOOK_SECRET": "",
        "SUBMISSION_SOURCE_ID": "newsagent_scheduled_source",
    }

    for key, value in defaults.items():
        os.environ.setdefault(key, value)