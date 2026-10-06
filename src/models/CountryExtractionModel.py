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


class CountryAndNamesExtractionModel(CountryExtractionModel):
    """
    Country extraction plus the names the Newsroom matches against its entity list.
    Used only when NEWSROOM_ENABLED is true; otherwise CountryExtractionModel is used unchanged.
    """
    companies: List[str] = Field(
        default_factory=list,
        description=(
            "Names of companies, developers, investors, banks, government bodies and other organisations "
            "the article is about, exactly as written in the article. No people, websites or social media handles."
        ),
    )
    projects: List[str] = Field(
        default_factory=list,
        description="Names of specific real-estate projects or developments, exactly as written in the article.",
    )
    places: List[str] = Field(
        default_factory=list,
        description=(
            "Cities, emirates, districts and communities mentioned, exactly as written in the article. "
            "Countries go in 'countries', not here."
        ),
    )
