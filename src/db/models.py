import uuid
from datetime import datetime, timezone
from typing import List, Optional, Any
from pydantic import BaseModel, Field
from src.db.enums import PromptStatus

# Since MongoDB is schema-less, we use Pydantic for application-side schema validation.

class PromptTemplate(BaseModel):
    """
    Represents a Prompt document in MongoDB.
    Collection: 'prompts'
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    name: str
    status: str = PromptStatus.DRAFT
    content: str
    description: Optional[str] = None
    input_variables: List[str] = Field(default_factory=list)
    version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        populate_by_name = True

class EmailRecipient(BaseModel):
    """
    Represents an Email Recipient document in MongoDB.
    Collection: 'email_recipients'
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    email: str
    name: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        populate_by_name = True

class Category(BaseModel):
    """
    Represents a News Category.
    Collection: 'categories'
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    name: str
    description: Optional[str] = None
    
    # --- NEW FIELD ---
    # Stores the ID from your Postgres User Profile service
    external_id: Optional[str] = Field(default=None, description="The matching ID from the Postgres User Profile service")
    # -----------------
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        populate_by_name = True