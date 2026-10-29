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
    topics: List[str] = Field(default_factory=list, description="This item's own real-estate topics")
    reason: str
    score: Optional[float] = Field(None, description="Only for similar-text matches")
    match: Literal["strong", "text", "topic"] = Field(
        "strong", description="strong: a shared company or specific place; text: similar text; topic: a shared "
                              "real-estate topic (text and topic: approved only, they fill the list when there are "
                              "fewer than 5 strong matches; with a Topic chosen, every approved item with it)")


class TopicCount(BaseModel):
    topic: str
    articles: int = Field(..., description="Approved articles with this topic (in the When window, if chosen)")
    signals: int = Field(..., description="Approved signals with this topic (in the When window, if chosen)")


class RelatedResponse(BaseModel):
    basis: Literal["organisations", "places", "similar", "topics", "none"] = Field(
        ..., description="What 'related' was based on: shared organisations, specific places, "
                         "similar text or shared topics only (no links), or nothing")
    items: List[RelatedItem]
    total: int
    limit: int
    skip: int
    topic_counts: List[TopicCount] = Field(default_factory=list, description="For each of the item's topics (and "
                                           "the chosen one): approved articles and signals with it")


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
    topics: List[str] = Field(default_factory=list, description="The item's real-estate topics")


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
