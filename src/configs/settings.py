import os
from typing import Optional
from pydantic_settings import BaseSettings
from langchain_openai import ChatOpenAI
from opik.integrations.langchain import OpikTracer
from langchain_tavily import TavilySearch
from opik import Opik
import opik
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    # API Settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "News Article Extractor"

    # Server Settings
    HOST: str = "0.0.0.0"
    PORT: int = 8000
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

    # LangGraph Settings
    OPENAI_API_KEY: str = os.getenv('OPENAI_API_KEY')
    OPENAI_URL: str = os.getenv('OPENAI_URL')
    OPIK_API_KEY: str = os.getenv('OPIK_API_KEY')
    OPIK_WORKSPACE: str = os.getenv('OPIK_WORKSPACE')
    TAVILY_API_KEY: str = os.getenv('TAVILY_API_KEY')

    # Model Configuration
    MODEL_NAME: str = os.getenv('MODEL_NAME')
    MODEL_TEMPERATURE: float = float(os.getenv('MODEL_TEMPERATURE'))

    # --- Email / SMTP Configuration ---
    SMTP_SERVER: str = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", 587))
    SMTP_EMAIL: str = os.getenv("SMTP_EMAIL", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")

    # Webhook Configuration
    # The URL where the agent will POST the final JSON
    WEBHOOK_URL: Optional[str] = os.getenv('WEBHOOK_URL')
    # Optional secret to validate the request comes from this agent
    WEBHOOK_SECRET: Optional[str] = os.getenv('WEBHOOK_SECRET')

    # Scheduler Configuration
    # Main API URL for submitting jobs (used by scheduler service)
    MAIN_API_URL: str = os.getenv('MAIN_API_URL', 'http://localhost:8000')
    # Source ID for manually submitted articles (not from scheduled sources)
    SUBMISSION_SOURCE_ID: str = os.getenv('SUBMISSION_SOURCE_ID', 'newsagent_scheduled_source')

    # Opik Settings
    def get_opik_client(self):
        if not self.OPIK_API_KEY:
            raise ValueError("OPIK_API_KEY is not set")
        if not self.OPIK_WORKSPACE:
            raise ValueError("OPIK_WORKSPACE is not set")

        opik.configure(
            api_key=self.OPIK_API_KEY,
            workspace=self.OPIK_WORKSPACE
        )

        client = Opik()
        opik_tracer = OpikTracer()

        return opik_tracer

    def get_model(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self.MODEL_NAME,
            temperature=self.MODEL_TEMPERATURE,
            openai_api_key=self.OPENAI_API_KEY,
            base_url=self.OPENAI_URL
        )

    def get_tavily_tool(self, max_results: int = 5) -> TavilySearch:
        if not self.TAVILY_API_KEY:
            raise ValueError("TAVILY_API_KEY is not set")

        return TavilySearch(
            api_key=self.TAVILY_API_KEY,
            max_results=max_results
        )

    class Config:
        env_file = ".env"
        case_sensitive = True
        env_file_encoding = 'utf-8'
        extra = "ignore"

settings = Settings()