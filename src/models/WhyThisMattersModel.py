from typing import List, Optional

from pydantic import BaseModel, Field


class ContextualReferenceModel(BaseModel):
    title: Optional[str] = None
    url: str
    source: Optional[str] = None
    snippet: Optional[str] = None


class WhyThisMattersModel(BaseModel):
    heading: str = "Why this matters"
    content: str = Field(
        ...,
        description="A neutral contextual paragraph (110-130 words) explaining why the article matters."
    )
    content_ar: Optional[str] = Field(
        None,
        description="Arabic translation of the Why this matters content."
    )
    references: List[ContextualReferenceModel] = Field(default_factory=list)


class WhyThisMattersLLMOutput(BaseModel):
    content: str = Field(
        ...,
        description="A neutral paragraph of 110-130 words (5-6 sentences of about 20-22 words each) for the 'Why this matters' section."
    )
