from pydantic import BaseModel, Field
from typing import List

class SearchQueryModel(BaseModel):
    """
    Holds the structured output for generated search queries.
    """
    keywords: List[str] = Field(
        ...,
        description="A list of 5-7 key terms or entities from the article."
    )
    queries: List[str] = Field(
        ...,
        description=(
            "A list of 3-5 diverse search queries to find "
            "corroborating articles."
        )
    )