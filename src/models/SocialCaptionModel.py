from pydantic import BaseModel, Field
from typing import List


class PlatformCaptionModel(BaseModel):
    """
    Caption variant for a specific social platform.
    """
    platform: str = Field(..., description="The target social platform.")
    headline: str = Field(..., description="Platform-specific hook or headline.")
    caption_body: str = Field(..., description="Platform-specific caption text.")
    hashtags: List[str] = Field(default_factory=list, description="Platform-specific hashtags.")
    call_to_action: str = Field(..., description="Platform-specific call to action.")


class SocialCaptionModel(BaseModel):
    """
    Structured output for the Social Media Caption Node.
    """
    headline: str = Field(..., description="A catchy, click-inducing hook or headline.")
    caption_body: str = Field(..., description="The main caption text, optimized for high conversion and engagement.")
    hashtags: List[str] = Field(..., description="A list of 5-10 relevant, high-traffic hashtags.")
    call_to_action: str = Field(..., description="A direct command to download the app or visit the website.")
    platform_captions: List[PlatformCaptionModel] = Field(
        default_factory=list,
        description="Caption variants for each requested platform."
    )
