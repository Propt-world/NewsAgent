from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from src.configs.settings import settings
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

    # Maps category names to their external IDs (e.g., {"Market": "pg_123"})
    category_mapping: Dict[str, str] = Field(default_factory=dict)

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

    # --- Newsroom (only set when NEWSROOM_ENABLED) ---
    # Names found by extract_country: {"companies": [...], "projects": [...], "places": [...]}.
    # Sent with the webhook result, never stored on the article itself.
    newsroom_names: Optional[Dict[str, List[str]]] = None

    # --- Real-estate check (scheduler jobs only, when REAL_ESTATE_CHECK_ENABLED) ---
    # Set by the scheduler for articles it found; manual submissions and "process anyway" leave it off.
    check_real_estate: bool = False
    # {"is_real_estate": bool, "reason": str, "model": str, "checked_at": iso}; None = not checked
    real_estate_check: Optional[Dict] = None

    # --- Global Error & Config Fields ---
    max_retries: int = 3
    # Summary length the critic step counts in code (the AI cannot count words reliably)
    summary_min_words: int = Field(default_factory=lambda: settings.SUMMARY_MIN_WORDS)
    summary_max_words: int = Field(default_factory=lambda: settings.SUMMARY_MAX_WORDS)
    error_message: Optional[str] = None