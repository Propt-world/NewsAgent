from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

# 1. NEW: Create a specific model for the LLM Generation
# This contains ONLY the fields the AI should generate.
class SeoLLMOutput(BaseModel):
    meta_title: str = Field(..., description="SEO optimized title, max 60 chars")
    meta_description: str = Field(..., description="SEO optimized description, max 160 chars")
    slug: str = Field(..., description="URL-friendly slug")
    primary_keywords: List[str] = Field(..., description="3-5 focus keywords")

    # Social Meta Tags
    og_title: str = Field(..., description="Open Graph Title")
    og_description: str = Field(..., description="Open Graph Description")
    twitter_card_title: str = Field(..., description="Twitter Card Title")
    twitter_card_description: str = Field(..., description="Twitter Card Description")

# 2. UPDATE: The main model inherits from the LLM output
# This adds the fields that are calculated in Python (json_ld_schema)
class SeoMetadataModel(SeoLLMOutput):
    """
    The complete model used in the application state.
    """
    # This field is excluded from the LLM generation step
    json_ld_schema: Dict[str, Any] = Field(default_factory=dict)