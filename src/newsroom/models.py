from datetime import datetime, timezone
from typing import Annotated, Dict, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, Field


def _as_utc(value: datetime) -> datetime:
    # MongoDB returns UTC times without a timezone; mark them as UTC so browsers convert them correctly.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]

EntityKind = Literal["organisation", "location"]
# "merged" is set only by a merge; reviewers choose between the other three.
EntityStatus = Literal["active", "staged", "ignored", "merged"]
EditableStatus = Literal["active", "staged", "ignored"]


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
    merged_into: Optional[str] = None
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
    status: EditableStatus = "staged"
    parent_id: Optional[str] = None
    country_id: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=2000)
    aliases: List[str] = Field(default_factory=list, max_length=50)
    ambiguous_aliases: List[str] = Field(default_factory=list, max_length=50)


class EntityUpdate(BaseModel):
    """Only the fields that are sent are changed; send parent_id/country_id as null to clear them."""
    type: Optional[str] = Field(None, min_length=1, max_length=64)
    canonical_name: Optional[str] = Field(None, min_length=1, max_length=200)
    status: Optional[EditableStatus] = None
    parent_id: Optional[str] = None
    country_id: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=2000)


class AliasCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=200)
    ambiguous: bool = False


class MergeRequest(BaseModel):
    into_id: str = Field(..., min_length=1, description="The entity that is kept")
    dry_run: bool = Field(False, description="Only count what would change")


class MergeResult(BaseModel):
    dry_run: bool
    merged_id: str
    into_id: str
    aliases_moved: int
    aliases_already_there: int
    links_moved: int
    links_both_decided: int = Field(..., description="Items where both had a decision; the kept entity's decision stays")
    suggestions_moved: int
    children_moved: int
    countries_moved: int
    entity: EntityOut


class DeleteRequest(BaseModel):
    confirm: Optional[str] = Field(None, description='Must be "delete" (any case) unless dry_run')
    dry_run: bool = Field(False, description="Only count what would be removed")


class DeleteResult(BaseModel):
    dry_run: bool
    entity_id: str
    canonical_name: str
    aliases_removed: int
    links_removed: int = Field(..., description="Links to articles and signals")
    decisions_removed: int = Field(..., description="Reviewer removals remembered for this entry")
    suggestions_reset: int = Field(..., description="Open suggestions that go back to 'not in the list'")


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


class FilteredItemsResponse(BaseModel):
    items: List[HistoryItem]
    total: int
    limit: int
    skip: int


class SourceRef(BaseModel):
    source_type: SourceType
    source_id: str = Field(..., min_length=1, max_length=100)


class ComposeRequest(BaseModel):
    sources: List[SourceRef] = Field(..., min_length=2, max_length=5)
    output_type: Literal["article", "signal"]
    exclude_entity_ids: List[str] = Field(default_factory=list, max_length=100,
                                          description="Companies/places the reviewer removed before composing")
    include_entity_ids: List[str] = Field(default_factory=list, max_length=20,
                                          description="Switched-on companies/places the reviewer added before composing")


class ExtractSignalRequest(BaseModel):
    article_id: str = Field(..., min_length=1, max_length=100)
    exclude_entity_ids: List[str] = Field(default_factory=list, max_length=100)
    include_entity_ids: List[str] = Field(default_factory=list, max_length=20)


class SignalSavedRequest(BaseModel):
    signal_id: str = Field(..., min_length=1, max_length=100)


class ComposeSourceOut(BaseModel):
    source_type: SourceType
    source_id: str
    title: Optional[str] = None
    url: Optional[str] = None
    status_at_request: Optional[str] = None


class ComposeRunOut(BaseModel):
    id: str
    kind: Literal["compose", "extract_signal"]
    output_type: Literal["article", "signal"]
    status: Literal["queued", "running", "succeeded", "not_a_signal", "failed"]
    sources: List[ComposeSourceOut]
    entity_ids: List[str] = []
    excluded_entity_ids: List[str] = []
    included_entity_ids: List[str] = []
    requested_by: Optional[str] = None
    created_at: UtcDatetime
    started_at: Optional[UtcDatetime] = None
    finished_at: Optional[UtcDatetime] = None
    model: Optional[str] = None
    prompts: Dict[str, str] = {}
    tokens: Dict[str, int] = {}
    cost_usd: float = 0.0
    result: Optional[dict] = None
    error: Optional[str] = None
    saved: Optional[dict] = None
    source_rejected: bool = False
    source_rejected_key: Optional[str] = None


class ComposeRunList(BaseModel):
    items: List[ComposeRunOut]
    spent_today_usd: float
    daily_budget_usd: float


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
