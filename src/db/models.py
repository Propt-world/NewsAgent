import uuid
from sqlalchemy import Column, String, Text, DateTime, JSON, Boolean
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
from src.db.enums import PromptStatus

Base = declarative_base()

class PromptTemplate(Base):
    __tablename__ = "prompts"

    # UUID Primary Key
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # Logical name (e.g. "summary_retry_user")
    name = Column(String, index=True, nullable=False)

    # Status: 'active', 'draft', or 'archived'
    status = Column(String, default=PromptStatus.DRAFT, nullable=False)

    # The prompt text with {{placeholders}}
    content = Column(Text, nullable=False)

    # Description of what this prompt does
    description = Column(String, nullable=True)

    # List of variables required (e.g. ["feedback", "article_text"])
    input_variables = Column(JSON, default=list)

    # Semantic versioning (e.g. "v1.0")
    version = Column(String, nullable=False)

    # Auto-timestamp
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class EmailRecipient(Base):
    __tablename__ = "email_recipients"

    # UUID Primary Key
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # Email address
    email = Column(String, nullable=False, unique=True)

    # Name of the recipient
    name = Column(String, nullable=True)

    # Whether the recipient is active
    is_active = Column(Boolean, default=True)

    # Auto-timestamp
    created_at = Column(DateTime(timezone=True), server_default=func.now())