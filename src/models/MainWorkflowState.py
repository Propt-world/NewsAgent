from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from src.models.ArticleModel import ArticleModel
from src.models.ValidationResultModel import ValidationResultModel
from src.models.SummaryAttemptModel import SummaryAttemptModel # <-- IMPORT

class MainWorkflowState(BaseModel):
    source_url: str
    cleaned_article_text: Optional[str] = None
    cleaned_article_html: Optional[str] = None
    news_article: Optional[ArticleModel] = None

    # --- Fields for Looping & Validation ---
    validation_count: int = 0 # This will be set by len(summary_attempts)

    # This holds the *latest* result for the conditional edge
    validation_result: Optional[ValidationResultModel] = None

    # --- NEW: This list will store all attempts ---
    summary_attempts: List[SummaryAttemptModel] = Field(default_factory=list)

    # --- Fields for Other Nodes ---
    other_sources: List[Dict] = Field(default_factory=list)

    # --- Global Error & Config Fields ---
    max_retries: int = 3
    error_message: Optional[str] = None