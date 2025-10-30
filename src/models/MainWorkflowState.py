from typing import Optional, List, Dict
from src.models.ArticleModel import ArticleModel
from src.models.ValidationResultModel import ValidationResultModel
from pydantic import BaseModel, Field

class MainWorkflowState(BaseModel):
    source_url: str

    cleaned_article_text: Optional[str] = None
    cleaned_article_html: Optional[str] = None

    news_article: Optional[ArticleModel] = None

    # --- Fields for Looping & Validation ---
    validation_count: int = 0
    validation_result: Optional[ValidationResultModel] = None

    # --- Fields for Other Nodes ---
    other_sources: List[Dict] = Field(default_factory=list)

    # --- Global Error & Config Fields ---
    max_retries: int = 3
    error_message: Optional[str] = None