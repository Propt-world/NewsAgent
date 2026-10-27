"""Related items, an entity's history and the Newsroom list filter by company or place."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from src.newsroom.models.common import SourceType, UtcDatetime


class EntityRef(BaseModel):
    id: str
    name: str


class RelatedItem(BaseModel):
    source_type: SourceType
    source_id: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    url: Optional[str] = None
    timestamp: Optional[UtcDatetime] = None
    item_status: Optional[str] = None
    shared_entities: List[EntityRef]
    shared_places: List[EntityRef]
    shared_topics: List[str] = Field(default_factory=list, description="Real-estate topics both items have")
    reason: str
    score: Optional[float] = Field(None, description="Only for similar-text matches")


class RelatedResponse(BaseModel):
    basis: Literal["organisations", "places", "similar", "none"] = Field(
        ..., description="What 'related' was based on: shared organisations, specific places, "
                         "similar text (approved articles with no links), or nothing")
    items: List[RelatedItem]
    total: int
    limit: int
    skip: int


class HistoryEntity(BaseModel):
    id: str
    name: str
    kind: str
    status: str


class HistoryItem(BaseModel):
    source_type: SourceType
    source_id: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    url: Optional[str] = None
    timestamp: Optional[UtcDatetime] = None
    item_status: Optional[str] = None
    location_ids: List[str] = []


class HistoryResponse(BaseModel):
    entity: HistoryEntity
    items: List[HistoryItem]
    total: int
    limit: int
    skip: int


class FilteredItemsResponse(BaseModel):
    items: List[HistoryItem]
    total: int
    limit: int
    skip: int
