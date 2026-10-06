from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

EntityKind = Literal["organisation", "location"]
EntityStatus = Literal["active", "staged", "ignored"]


class NewsroomHealthResponse(BaseModel):
    status: str = Field(..., examples=["healthy"])
    service: str = Field(default="newsroom", examples=["newsroom"])
    timestamp: datetime


class AliasOut(BaseModel):
    id: str
    alias: str
    alias_norm: str
    ambiguous: bool
    lang: Optional[str] = None


class EntityOut(BaseModel):
    id: str
    kind: EntityKind
    type: str
    status: EntityStatus
    canonical_name: str
    parent_id: Optional[str] = None
    path_ids: List[str] = []
    country_id: Optional[str] = None
    context_slug: Optional[str] = None
    notes: Optional[str] = None
    mentions_in_data: int = 0
    aliases: List[AliasOut] = []
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class EntityListResponse(BaseModel):
    items: List[EntityOut]
    total: int
    limit: int
    skip: int


class EntityCreate(BaseModel):
    kind: EntityKind
    type: str = Field(..., min_length=1, max_length=64)
    canonical_name: str = Field(..., min_length=1, max_length=200)
    status: EntityStatus = "staged"
    parent_id: Optional[str] = None
    country_id: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=2000)
    aliases: List[str] = Field(default_factory=list, max_length=50)
    ambiguous_aliases: List[str] = Field(default_factory=list, max_length=50)


class EntityUpdate(BaseModel):
    """Only the fields that are sent are changed; send parent_id/country_id as null to clear them."""
    type: Optional[str] = Field(None, min_length=1, max_length=64)
    canonical_name: Optional[str] = Field(None, min_length=1, max_length=200)
    status: Optional[EntityStatus] = None
    parent_id: Optional[str] = None
    country_id: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=2000)


class AliasCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=200)
    ambiguous: bool = False


class IgnoreOut(BaseModel):
    id: str
    string: str
    reason: Optional[str] = None
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None


class IgnoreCreate(BaseModel):
    string: str = Field(..., min_length=1, max_length=200)
    reason: Optional[str] = Field(None, max_length=500)


class AuditOut(BaseModel):
    id: str
    at: datetime
    actor: str
    action: str
    entity_id: Optional[str] = None
    before: Optional[dict] = None
    after: Optional[dict] = None
