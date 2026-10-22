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

    # Summary and "why this matters" lengths: filled into the registry prompts' {summary_min_words}-style
    # placeholders, and the summary's is also counted in code by the critic step. Change them here, not in code.
    SUMMARY_MIN_WORDS: int = int(os.getenv("SUMMARY_MIN_WORDS", "110"))
    SUMMARY_MAX_WORDS: int = int(os.getenv("SUMMARY_MAX_WORDS", "130"))
    WTM_MIN_WORDS: int = int(os.getenv("WTM_MIN_WORDS", "110"))
    WTM_MAX_WORDS: int = int(os.getenv("WTM_MAX_WORDS", "130"))

    # Search / Context Configuration
    SEARCH_PROVIDER: str = os.getenv("SEARCH_PROVIDER", "searxng")
    SEARXNG_BASE_URL: str = os.getenv("SEARXNG_BASE_URL", "http://localhost:8080")
    SEARXNG_ENGINES: str = os.getenv("SEARXNG_ENGINES", "duckduckgo,brave")
    SEARCH_MAX_RESULTS: int = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
    SEARCH_TIMEOUT_SECONDS: float = float(os.getenv("SEARCH_TIMEOUT_SECONDS", "8"))
    SEARCH_CACHE_TTL_SECONDS: int = int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "86400"))
    SEARCH_LANGUAGE: str = os.getenv("SEARCH_LANGUAGE", "en")
    SEARCH_SAFESEARCH: int = int(os.getenv("SEARCH_SAFESEARCH", "1"))

    # Social caption defaults used when prompts request platform variants.
    SOCIAL_PLATFORMS: str = os.getenv("SOCIAL_PLATFORMS", "X, Instagram, LinkedIn, Facebook")
    SOCIAL_CTA_TARGET: str = os.getenv("SOCIAL_CTA_TARGET", "download the Propt App for the full analysis")

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

    # Newsroom Configuration
    # Off unless explicitly enabled; when off no /newsroom routes are mounted.
    # Kept as a string so an empty or unexpected value means "off" instead of a startup error.
    NEWSROOM_ENABLED: str = os.getenv('NEWSROOM_ENABLED', 'false')
    # Real-estate check before processing articles the scheduler found (true to enable; off by default)
    REAL_ESTATE_CHECK_ENABLED: str = os.getenv('REAL_ESTATE_CHECK_ENABLED', 'false')
    # A classifier: steady answers, so a low temperature (separate from MODEL_TEMPERATURE)
    REAL_ESTATE_CHECK_TEMPERATURE: float = float(os.getenv('REAL_ESTATE_CHECK_TEMPERATURE', '0'))
    # How much of the fetched article text the check reads (with the title)
    REAL_ESTATE_CHECK_CHARS: int = int(os.getenv('REAL_ESTATE_CHECK_CHARS', '1500'))
    # The Newsroom's own collections (graph, compose jobs) live in MONGO_DB_NAME with the articles
    # (user decision 2026-10-19). social_scrapper's signals are read from this database (same cluster).
    NEWSROOM_SIGNALS_DB_NAME: str = os.getenv('NEWSROOM_SIGNALS_DB_NAME', 'newsroom')
    # social_scrapper's signals collection in that database (read-only for NewsAgent)
    NEWSROOM_SIGNALS_COLLECTION: str = os.getenv('NEWSROOM_SIGNALS_COLLECTION', 'signals')
    # "Similar articles" (approved articles only): minimum cosine score to show a match
    NEWSROOM_SIMILARITY_MIN_SCORE: float = float(os.getenv('NEWSROOM_SIMILARITY_MIN_SCORE', '0.8'))
    # Compose (Propt-authored articles and signals): model and spending limits (B1, B2)
    NEWSROOM_COMPOSE_MODEL: str = os.getenv('NEWSROOM_COMPOSE_MODEL', 'gpt-4o-mini')
    # Compose temperatures, separate from MODEL_TEMPERATURE (the normal pipeline steps).
    # Writer (the article or signal text): livelier wording.
    NEWSROOM_COMPOSE_TEMPERATURE: float = float(os.getenv('NEWSROOM_COMPOSE_TEMPERATURE', '0.7'))
    # Researcher, Editor and the extract-signal decision: facts and checks, same as the pipeline's extraction.
    NEWSROOM_COMPOSE_CHECK_TEMPERATURE: float = float(os.getenv('NEWSROOM_COMPOSE_CHECK_TEMPERATURE', '0.5'))
    NEWSROOM_COMPOSE_PER_HOUR: int = int(os.getenv('NEWSROOM_COMPOSE_PER_HOUR', '10'))
    NEWSROOM_COMPOSE_CONCURRENT: int = int(os.getenv('NEWSROOM_COMPOSE_CONCURRENT', '2'))
    NEWSROOM_DAILY_BUDGET_USD: float = float(os.getenv('NEWSROOM_DAILY_BUDGET_USD', '10'))
    NEWSROOM_BUDGET_TIMEZONE: str = os.getenv('NEWSROOM_BUDGET_TIMEZONE', 'Asia/Dubai')
    # Held back from the daily budget for each queued or running job, so jobs in progress cannot overshoot it.
    NEWSROOM_COMPOSE_RESERVE_USD: float = float(os.getenv('NEWSROOM_COMPOSE_RESERVE_USD', '0.05'))
    # Used when the price list does not know the model (it would report $0 and the cap would never stop
    # anything). Default: gpt-4o's price per 1M tokens, so the cap errs on the safe side.
    NEWSROOM_FALLBACK_INPUT_USD_PER_1M: float = float(os.getenv('NEWSROOM_FALLBACK_INPUT_USD_PER_1M', '2.50'))
    NEWSROOM_FALLBACK_OUTPUT_USD_PER_1M: float = float(os.getenv('NEWSROOM_FALLBACK_OUTPUT_USD_PER_1M', '10.00'))
    # Counted from when the worker starts the job, not from when it was requested.
    NEWSROOM_COMPOSE_TIMEOUT_MINUTES: int = int(os.getenv('NEWSROOM_COMPOSE_TIMEOUT_MINUTES', '15'))
    # How long a job may wait in the queue (busy or restarting worker) before it is given up; nothing is charged.
    NEWSROOM_COMPOSE_MAX_WAIT_HOURS: float = float(os.getenv('NEWSROOM_COMPOSE_MAX_WAIT_HOURS', '6'))
    # A job cut off by a worker shutdown is put back in the queue, at most this many starts in total.
    NEWSROOM_COMPOSE_MAX_ATTEMPTS: int = int(os.getenv('NEWSROOM_COMPOSE_MAX_ATTEMPTS', '3'))
    NEWSROOM_COMPOSE_QUEUE: str = os.getenv('NEWSROOM_COMPOSE_QUEUE', 'newsroom_compose_jobs')
    # Alerts (banner in the backoffice + "NEWSROOM_ALERT <code>: ..." log lines for CloudWatch).
    # The scheduler checks every NEWSROOM_ALERT_CHECK_MINUTES; the compose worker reports in every 30 s.
    NEWSROOM_ALERT_CHECK_MINUTES: int = int(os.getenv('NEWSROOM_ALERT_CHECK_MINUTES', '10'))
    NEWSROOM_ALERT_WORKER_MINUTES: int = int(os.getenv('NEWSROOM_ALERT_WORKER_MINUTES', '5'))
    NEWSROOM_ALERT_QUEUE_MINUTES: int = int(os.getenv('NEWSROOM_ALERT_QUEUE_MINUTES', '10'))
    NEWSROOM_ALERT_BUDGET_SHARE: float = float(os.getenv('NEWSROOM_ALERT_BUDGET_SHARE', '0.8'))
    # Real-estate check (when on): alert when more than this share of the last 24 h's articles was filtered out
    NEWSROOM_ALERT_FILTERED_SHARE: float = float(os.getenv('NEWSROOM_ALERT_FILTERED_SHARE', '0.5'))

    @property
    def newsroom_enabled(self) -> bool:
        return self.NEWSROOM_ENABLED.strip().lower() in ("1", "true", "yes", "on")

    @property
    def real_estate_check_enabled(self) -> bool:
        return self.REAL_ESTATE_CHECK_ENABLED.strip().lower() in ("1", "true", "yes", "on")

    # Scheduler Configuration
    # Main API URL for submitting jobs (used by scheduler service)
    MAIN_API_URL: str = os.getenv('MAIN_API_URL', 'http://localhost:8003')
    # Source ID for manually submitted articles (not from scheduled sources)
    SUBMISSION_SOURCE_ID: str = os.getenv('SUBMISSION_SOURCE_ID', 'newsagent_scheduled_source')
    # Scheduler URL for health checking
    SCHEDULER_URL: str = os.getenv('SCHEDULER_URL', 'http://scheduler:8001')

    # Opik Settings
    def get_opik_client(self, graph=None, **tracer_options):
        """tracer_options (optional): extra OpikTracer settings such as tags, metadata or thread_id."""
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
            opik_tracer = OpikTracer(graph=graph, project_name=self.OPIK_PROJECT_NAME, **tracer_options)
        else:
            opik_tracer = OpikTracer(project_name=self.OPIK_PROJECT_NAME, **tracer_options)

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
