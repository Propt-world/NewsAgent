from pydantic import BaseModel, Field
from typing import Optional

class TranslationModel(BaseModel):
    """
    Structured output for the Translation Node.
    """
    title_ar: str = Field(..., description="The article title translated to Modern Standard Arabic.")
    summary_ar: str = Field(
        ...,
        description="The complete article summary translated to Arabic, sentence by sentence: nothing shortened or left out.",
    )
    why_this_matters_ar: Optional[str] = Field(
        None,
        description="The Why this matters sentence translated to Arabic."
    )
    content_ar: str = Field(..., description="The full article content translated to Arabic.")
