from pydantic import BaseModel, Field
from typing import Optional

class ValidationResultModel(BaseModel):
    """
    Holds the structured output from the 'validate_summary' (Critic) node.
    """
    # Pass/fail flag for the conditional loop
    is_valid: bool = False

    # Qualitative feedback for the regeneration loop
    feedback: str = "Validation not yet run."

    # Your new quantitative scores
    semantic_score: Optional[float] = Field(
        None,
        description="Score (0.0-10.0) for semantic similarity to the original article."
    )
    tone_score: Optional[float] = Field(
        None,
        description="Score (0.0-10.0) for tone alignment with the original article."
    )