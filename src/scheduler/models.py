import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, HttpUrl

class SourceConfig(BaseModel):
    """
    Configuration for a News Source (e.g., 'CNN Real Estate').
    Stored in 'sources' collection.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    name: str
    listing_url: str
    # Regex or substring to filter links (e.g., "/real-estate/")
    # If None, it accepts all links from the domain.
    url_pattern: Optional[str] = None

    # How often to check this source (in minutes)
    fetch_interval_minutes: int = 60

    is_active: bool = True
    last_run_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.now(timezone.utc))

    class Config:
        populate_by_name = True

class ProcessedArticle(BaseModel):
    """
    The 'Memory' of the system. Tracks every URL discovered.
    Stored in 'processed_articles' collection.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    source_id: str
    url: str

    # Status: 'discovered', 'queued', 'completed', 'failed'
    status: str = "discovered"

    discovered_at: datetime = Field(default_factory=datetime.now(timezone.utc))
    processed_at: Optional[datetime] = None

    # The final output from the AI Worker (Summary, Translation, SEO, etc.)
    final_output: Optional[Dict[str, Any]] = None

    class Config:
        populate_by_name = True