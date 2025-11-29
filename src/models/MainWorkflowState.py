from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from src.models.ArticleModel import ArticleModel
from src.models.AgentPromptsModel import AgentPromptsModel
from src.models.ValidationResultModel import ValidationResultModel
from src.models.SummaryAttemptModel import SummaryAttemptModel
from src.models.SearchQueryModel import SearchQueryModel

class MainWorkflowState(BaseModel):
    source_url: str
    cleaned_article_text: Optional[str] = None
    cleaned_article_html: Optional[str] = None
    news_article: Optional[ArticleModel] = None

    # The active set of prompts currently used by the agent in this workflow instance.
    # This may be loaded from the database or constructed during workflow initialization.
    active_prompts: Optional[AgentPromptsModel] = None

    # --- Fields for Looping & Validation ---
    validation_count: int = 0

    # --- Fields for Validation ---
    validation_result: Optional[ValidationResultModel] = None

    # --- Fields for Summary Generation ---
    summary_attempts: List[SummaryAttemptModel] = Field(default_factory=list)

    # --- Fields for Other Nodes ---
    other_sources: List[Dict] = Field(default_factory=list)

    # --- Fields for Search Query Generation ---
    search_query_data: Optional[SearchQueryModel] = None

    # --- Global Error & Config Fields ---
    max_retries: int = 3
    error_message: Optional[str] = None