from pydantic import BaseModel, Field
from typing import List

class SocialCaptionModel(BaseModel):
    """
    Structured output for the Social Media Caption Node.
    """
    headline: str = Field(..., description="A catchy, click-inducing hook or headline.")
    caption_body: str = Field(..., description="The main caption text, optimized for high conversion and engagement.")
    hashtags: List[str] = Field(..., description="A list of 5-10 relevant, high-traffic hashtags.")
    call_to_action: str = Field(..., description="A direct command to download the app or visit the website.")