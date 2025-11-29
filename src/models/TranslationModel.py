from pydantic import BaseModel, Field

class TranslationModel(BaseModel):
    """
    Structured output for the Translation Node.
    """
    title_ar: str = Field(..., description="The article title translated to Modern Standard Arabic.")
    summary_ar: str = Field(..., description="The article summary translated to Arabic.")
    content_ar: str = Field(..., description="The full article content translated to Arabic.")