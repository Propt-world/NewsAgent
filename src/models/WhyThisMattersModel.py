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
        description="A neutral contextual paragraph explaining why the article matters."
    )
    references: List[ContextualReferenceModel] = Field(default_factory=list)


class WhyThisMattersLLMOutput(BaseModel):
    content: str = Field(
        ...,
        description="A single neutral paragraph for the 'Why this matters' section."
    )
