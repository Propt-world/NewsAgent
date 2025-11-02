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

    # LangGraph Settings
    OPENAI_API_KEY: str = os.getenv('OPENAI_API_KEY')
    OPENAI_URL: str = os.getenv('OPENAI_URL')
    OPIK_API_KEY: str = os.getenv('OPIK_API_KEY')
    OPIK_WORKSPACE: str = os.getenv('OPIK_WORKSPACE')
    TAVILY_API_KEY: str = os.getenv('TAVILY_API_KEY')

    # Model Configuration
    MODEL_NAME: str = os.getenv('MODEL_NAME')
    MODEL_TEMPERATURE: float = float(os.getenv('MODEL_TEMPERATURE'))

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