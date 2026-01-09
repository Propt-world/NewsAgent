from typing import List
from pydantic import BaseModel, Field

class CountryExtractionModel(BaseModel):
    """
    Structured output model for country extraction.
    """
    countries: List[str] = Field(
        default_factory=list,
        description="A list of countries mentioned or relevant to the article content."
    )
