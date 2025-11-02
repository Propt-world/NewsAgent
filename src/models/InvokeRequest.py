from pydantic import BaseModel, Field

class InvokeRequest(BaseModel):
    """
    The simple request model for our API.
    The client only needs to provide this.
    """
    source_url: str = Field(..., description="The URL of the news article to process.")

    max_retries: int = Field(
        default=3,
        description="Optional: Max attempts for the summary loop."
    )