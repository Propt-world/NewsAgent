import os
from typing import Optional, List
from pydantic_settings import BaseSettings
from langchain_openai import ChatOpenAI
from opik.integrations.langchain import OpikTracer
from opik import Opik
import opik
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    # API Settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "News Article Extractor"
    
    # CORS Configuration
    CORS_ORIGINS: str = "https://main.d211u21suwdysn.amplifyapp.com,http://localhost:3000,http://localhost:3003,http://localhost:3004,http://localhost:3005,http://localhost:3006,http://localhost:5173,http://localhost:8003,http://localhost:8001,https://backoffice.propt.global"

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    # Server Settings
    HOST: str = "0.0.0.0"
    PORT: int = 8003
    RELOAD: bool = True

    # Redis Configuration
    # Default to localhost for dev, but configurable via .env
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_QUEUE_NAME: str = os.getenv("REDIS_QUEUE_NAME", "newsagent_jobs")
    # Dead Letter Queue Configuration
    REDIS_DLQ_NAME: str = os.getenv("REDIS_DLQ_NAME", "newsagent_dlq")

    # MongoDB Settings
    # Default to local mongodb
    DATABASE_URL: str = os.getenv('DATABASE_URL', "mongodb://localhost:27017")
    MONGO_DB_NAME: str = os.getenv('MONGO_DB_NAME', "newsagent")
    
    # Vector DB / Embedding Settings (Required by mongo_store.py)
    # Aligning MONGODB_DB with MONGO_DB_NAME, but verifying expected variable name
    MONGO_DB_COLLECTION: str = os.getenv('MONGO_DB_COLLECTION', "vectorized_articles")
    MONGO_DB_BIO_COLLECTION: str = os.getenv('MONGO_DB_BIO_COLLECTION', "chatbot_bio")
    MONGO_VECTOR_INDEX_NAME: str = os.getenv('MONGO_VECTOR_INDEX_NAME', "newsagent_vector_index")
    MONGO_EMBEDDING_MODEL: str = os.getenv('MONGO_EMBEDDING_MODEL', "text-embedding-3-large")


    # AWS S3 Settings
    AWS_ACCESS_KEY_ID: Optional[str] = os.getenv('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY: Optional[str] = os.getenv('AWS_SECRET_ACCESS_KEY')
    AWS_REGION: str = os.getenv('AWS_REGION', 'us-east-1')
    S3_BUCKET_NAME: Optional[str] = os.getenv('S3_BUCKET_NAME')
    S3_FOLDER_PREFIX: Optional[str] = os.getenv('S3_FOLDER_PREFIX', '')

    # Keys and URLs
    OPENAI_API_KEY: str = os.getenv('OPENAI_API_KEY')
    OPENAI_URL: str = os.getenv('OPENAI_URL')
    OPIK_API_KEY: str = os.getenv('OPIK_API_KEY')
    OPIK_WORKSPACE: str = os.getenv('OPIK_WORKSPACE')
    OPIK_PROJECT_NAME: str = os.getenv('OPIK_PROJECT_NAME')

    # Model Configuration
    MODEL_NAME: str = os.getenv('MODEL_NAME', "gpt-4o-mini")
    MODEL_TEMPERATURE: float = float(os.getenv('MODEL_TEMPERATURE', "0.5"))

    # Search / Context Configuration
    SEARCH_PROVIDER: str = os.getenv("SEARCH_PROVIDER", "searxng")
    SEARXNG_BASE_URL: str = os.getenv("SEARXNG_BASE_URL", "http://localhost:8080")
    SEARXNG_ENGINES: str = os.getenv("SEARXNG_ENGINES", "duckduckgo,brave")
    SEARCH_MAX_RESULTS: int = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
    SEARCH_TIMEOUT_SECONDS: float = float(os.getenv("SEARCH_TIMEOUT_SECONDS", "8"))
    SEARCH_CACHE_TTL_SECONDS: int = int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "86400"))
    SEARCH_LANGUAGE: str = os.getenv("SEARCH_LANGUAGE", "en")
    SEARCH_SAFESEARCH: int = int(os.getenv("SEARCH_SAFESEARCH", "1"))

    # Scraping Configuration
    # Generic User Agent to mimic a real browser/user to avoid bot blocks
    USER_AGENT: str = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # URL for Browserless (e.g., 'ws://browserless:3000')
    # If None, raises error in browser.py as we removed local fallback
    BROWSER_WS_ENDPOINT: Optional[str] = os.getenv('BROWSER_WS_ENDPOINT')
    # HTTP URL for health checks (e.g., 'http://browserless:3000')
    BROWSERLESS_URL: Optional[str] = os.getenv('BROWSERLESS_URL')
    BROWSERLESS_TOKEN: Optional[str] = os.getenv('BROWSERLESS_TOKEN')

    # Email / SMTP Configuration
    SMTP_SERVER: str = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", 587))
    SMTP_EMAIL: str = os.getenv("SMTP_EMAIL", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")

    # The master API Key for accessing endpoints
    NEWSAGENT_API_KEY: Optional[str] = os.getenv('NEWSAGENT_API_KEY')

    # Webhook Configuration
    # The URL where the agent will POST the final JSON
    WEBHOOK_URL: Optional[str] = os.getenv('WEBHOOK_URL')
    WEBHOOK_SECRET: Optional[str] = os.getenv('WEBHOOK_SECRET')

    # Scheduler Configuration
    # Main API URL for submitting jobs (used by scheduler service)
    MAIN_API_URL: str = os.getenv('MAIN_API_URL', 'http://localhost:8003')
    # Source ID for manually submitted articles (not from scheduled sources)
    SUBMISSION_SOURCE_ID: str = os.getenv('SUBMISSION_SOURCE_ID', 'newsagent_scheduled_source')
    # Scheduler URL for health checking
    SCHEDULER_URL: str = os.getenv('SCHEDULER_URL', 'http://scheduler:8001')

    # Opik Settings
    def get_opik_client(self, graph=None):
        if not self.OPIK_API_KEY:
            raise ValueError("OPIK_API_KEY is not set")
        if not self.OPIK_WORKSPACE:
            raise ValueError("OPIK_WORKSPACE is not set")

        opik.configure(
            api_key=self.OPIK_API_KEY,
            workspace=self.OPIK_WORKSPACE,
        )

        # If a graph is provided, pass it to the tracer for visualization
        if graph:
            opik_tracer = OpikTracer(graph=graph, project_name=self.OPIK_PROJECT_NAME)
        else:
            opik_tracer = OpikTracer(project_name=self.OPIK_PROJECT_NAME)

        return opik_tracer

    def get_model(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self.MODEL_NAME,
            temperature=self.MODEL_TEMPERATURE,
            openai_api_key=self.OPENAI_API_KEY
        )

    def get_search_client(self):
        provider = (self.SEARCH_PROVIDER or "").lower()
        if provider != "searxng":
            raise ValueError(f"Unsupported SEARCH_PROVIDER: {self.SEARCH_PROVIDER}")

        from src.utils.search import SearxngSearchClient

        return SearxngSearchClient(
            base_url=self.SEARXNG_BASE_URL,
            engines=self.SEARXNG_ENGINES,
            timeout_seconds=self.SEARCH_TIMEOUT_SECONDS,
            max_results=self.SEARCH_MAX_RESULTS,
            cache_ttl_seconds=self.SEARCH_CACHE_TTL_SECONDS,
            language=self.SEARCH_LANGUAGE,
            safesearch=self.SEARCH_SAFESEARCH,
        )

    class Config:
        env_file = ".env"
        case_sensitive = True
        env_file_encoding = 'utf-8'
        extra = "ignore"

settings = Settings()
