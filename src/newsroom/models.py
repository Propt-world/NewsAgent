from datetime import datetime, timezone
from typing import Annotated, Dict, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, Field


def _as_utc(value: datetime) -> datetime:
    # MongoDB returns UTC times without a timezone; mark them as UTC so browsers convert them correctly.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]

EntityKind = Literal["organisation", "location"]
EntityStatus = Literal["active", "staged", "ignored"]


class NewsroomHealthResponse(BaseModel):
    status: str = Field(..., examples=["healthy"])
    service: str = Field(default="newsroom", examples=["newsroom"])
    timestamp: UtcDatetime


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
    created_at: Optional[UtcDatetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[UtcDatetime] = None
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
    created_at: Optional[UtcDatetime] = None
    created_by: Optional[str] = None


class IgnoreCreate(BaseModel):
    string: str = Field(..., min_length=1, max_length=200)
    reason: Optional[str] = Field(None, max_length=500)


SourceType = Literal["article", "signal"]
SuggestionField = Literal["organisation", "location"]


class SuggestRequest(BaseModel):
    source_type: SourceType
    source_id: str = Field(..., min_length=1, max_length=100)
    organisations: List[str] = Field(default_factory=list, max_length=100)
    locations: List[str] = Field(default_factory=list, max_length=100)


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
    entity_id: str
    suggestion_id: Optional[str] = None
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
    name_norm: str
    entity_id: str
    activate: bool = False


class NewNameDismissRequest(BaseModel):
    field: SuggestionField
    name_norm: str


class CountResponse(BaseModel):
    count: int


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


class ItemsSummaryRequest(BaseModel):
    source_type: SourceType
    source_ids: List[str] = Field(..., min_length=1, max_length=200)


class ItemSummary(BaseModel):
    links: int
    open_suggestions: int


class ItemsSummaryResponse(BaseModel):
    items: Dict[str, ItemSummary]


class ItemsChangedRequest(BaseModel):
    source_type: SourceType
    source_ids: List[str] = Field(..., min_length=1, max_length=500)


class ItemsChangedResponse(BaseModel):
    updated: int
    missing: int


class AuditOut(BaseModel):
    id: str
    at: UtcDatetime
    actor: str
    action: str
    entity_id: Optional[str] = None
    before: Optional[dict] = None
    after: Optional[dict] = None
