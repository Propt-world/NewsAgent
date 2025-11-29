from pydantic import BaseModel, Field

class AgentPromptsModel(BaseModel):
    """
    Defines the exact set of prompts required for the NewsAgent to function.
    This ensures that if the DB is missing a key, we fail fast.
    """
    # --- Content Extraction ---
    content_extractor: str = Field(..., description="Prompt for the content extraction node")

    # --- Summarization ---
    summary_system: str
    summary_initial_user: str
    summary_retry_user: str

    # --- Validation (Critic) ---
    validation_system: str
    validation_user: str

    # --- Relevance ---
    relevance_system: str
    relevance_user: str

    # --- Search ---
    search_system: str
    search_user: str

    # --- Categorization ---
    categorization_system: str
    categorization_user: str

    # --- SEO ---
    seo_system: str
    seo_user: str