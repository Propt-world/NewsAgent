from pydantic import BaseModel, Field
from typing import Optional, List

class CategorizationModel(BaseModel):
    """
    Holds the structured output for the categorization LLM.
    """
    # --- Fields for Categorization ---
    categories: List[str] = Field(
        ...,
        description="A list of 3 to 4 categories from the knowledge base.",
        max_length=4
    )
    # --- END Fields for Categorization ---