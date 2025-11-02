from pydantic import BaseModel, Field

class RelevanceScoreModel(BaseModel):
    """
    Holds the structured output from the relevance scoring LLM.
    """
    # Score from 0.0 (irrelevant) to 10.0 (highly relevant)
    score: float = Field(
        ...,
        ge=0.0,
        le=10.0,
        description="The relevance score from 0.0 to 10.0."
    )
    # A brief justification for the score
    reason: str = Field(
        ...,
        description="A brief reason for the given score."
    )