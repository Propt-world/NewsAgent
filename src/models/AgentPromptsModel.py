from typing import Optional

from pydantic import BaseModel, Field

class AgentPromptsModel(BaseModel):
    """
    Defines the exact set of prompts required for the NewsAgent to function.
    This ensures that if the DB is missing a key, we fail fast.
    """
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

    # --- Country Extraction ---
    country_extraction_system: str
    country_extraction_user: str

    # --- Content Enrichment ---
    content_enrichment_system: Optional[str] = None
    content_enrichment_user: Optional[str] = None

    # --- SEO ---
    seo_system: str
    seo_user: str

    # --- Social Media Caption ---
    social_caption_system: str
    social_caption_user: str

    # --- Translation ---
    translation_system: str
    translation_user: str
