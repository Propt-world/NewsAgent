from pydantic import BaseModel, Field
from typing import Optional, List

class CategorizationModel(BaseModel):
    """
    Holds the structured output for the categorization LLM.
    """
    # --- Fields for Categorization ---
    categories: List[str] = Field(
        ...,
        description="A list of 1 to 3 main categories from the knowledge base.",
        max_length=3
    )

    sub_categories: List[str] = Field(
        default_factory=list,
        description="A list of all relevant sub-categories from the knowledge base."
    )
    # --- END Fields for Categorization ---