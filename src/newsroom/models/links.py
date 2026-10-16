"""Suggestions and links on articles and signals, the New Names queue, item summaries and changes."""
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from src.newsroom.models.common import ItemId, NameText, SourceType, SuggestionField, UtcDatetime


class SuggestRequest(BaseModel):
    source_type: SourceType
    source_id: str = Field(..., min_length=1, max_length=100)
    organisations: List[NameText] = Field(default_factory=list, max_length=100)
    locations: List[NameText] = Field(default_factory=list, max_length=100)


class SuggestResponse(BaseModel):
    created: int
    existing: int
    skipped_ignored: int
    skipped_handle: int
    skipped_ambiguous: int
    skipped_switched_off: int
    skipped_linked_or_removed: int


class SuggestionOut(BaseModel):
    id: str
    source_type: SourceType
    source_id: str
    field: SuggestionField
    name: str
    entity_id: Optional[str] = None
    entity_status: Optional[str] = None
    matched_alias: Optional[str] = None
    reason: str
    status: str
    in_new_names_queue: bool = False


class LinkOut(BaseModel):
    id: str
    entity_id: str
    kind: SuggestionField
    canonical_name: Optional[str] = None
    entity_type: Optional[str] = None
    entity_status: Optional[str] = None
    matched_alias: Optional[str] = None
    topics: List[str] = []
    location_ids: List[str] = []
    linked_by: Optional[str] = None
    linked_at: Optional[UtcDatetime] = None


class ItemEntitiesResponse(BaseModel):
    links: List[LinkOut]
    suggestions: List[SuggestionOut]


class LinkRequest(BaseModel):
    entity_id: ItemId
    suggestion_id: Optional[ItemId] = None
    activate: bool = Field(False, description="Required to link an entity that is switched off; it is switched on.")


class NewNameItem(BaseModel):
    source_type: SourceType
    source_id: str
    suggestion_id: str
    title: Optional[str] = None


class NewNameGroup(BaseModel):
    field: SuggestionField
    name_norm: str
    name: str
    count: int
    items: List[NewNameItem]


class NewNameLinkRequest(BaseModel):
    field: SuggestionField
    name_norm: NameText
    entity_id: ItemId
    activate: bool = False


class NewNameDismissRequest(BaseModel):
    field: SuggestionField
    name_norm: NameText


class CountResponse(BaseModel):
    count: int


class ItemsSummaryRequest(BaseModel):
    source_type: SourceType
    source_ids: List[ItemId] = Field(..., min_length=1, max_length=200)


class ItemSummary(BaseModel):
    links: int
    open_suggestions: int


class ItemsSummaryResponse(BaseModel):
    items: Dict[str, ItemSummary]


class ItemsChangedRequest(BaseModel):
    source_type: SourceType
    source_ids: List[ItemId] = Field(..., min_length=1, max_length=500)


class ItemsChangedResponse(BaseModel):
    updated: int
    missing: int
