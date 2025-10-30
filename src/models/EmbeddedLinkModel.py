from typing import Optional
from pydantic import BaseModel, Field

class EmbeddedLinkModel(BaseModel):
    # These fields are required. An "embedded link"
    # is not valid without its text and URL.
    hyperlink_text: str
    url: str

    # This is for the text *around* the link, which gives
    # context for the relevance check.
    context: Optional[str] = None

    # This is populated by a later node.
    relevance_score: Optional[float] = None