"""Compose: requests, desks, the source check, signal drafts and the compose history."""
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from src.newsroom.models.common import ItemId, SourceType, UtcDatetime


class SourceRef(BaseModel):
    source_type: SourceType
    source_id: str = Field(..., min_length=1, max_length=100)


# Optional article types (no type = the regular prompts). Required when the sources share no company or specific place.
ComposeType = Literal["roundup", "comparison", "update", "background"]


# Optional specialist desks for articles (desks.py); no desk = the general writer.
Desk = Literal["sales", "rental", "finance", "market", "policy", "projects", "commercial", "luxury", "communities"]


class ComposeRequest(BaseModel):
    sources: List[SourceRef] = Field(..., min_length=2, max_length=5)
    output_type: Literal["article", "signal"]
    compose_type: Optional[ComposeType] = Field(
        None, description="Articles only. Leave empty for a regular article; required when the sources share "
                          "no company or specific place (then update is not allowed).")
    desk: Optional[Desk] = Field(None, description="Articles only. A specialist desk; empty = the general writer.")
    exclude_entity_ids: List[ItemId] = Field(default_factory=list, max_length=100,
                                             description="Companies/places the reviewer removed before composing")
    include_entity_ids: List[ItemId] = Field(default_factory=list, max_length=20,
                                          description="Switched-on companies/places the reviewer added before composing")


class DeskSuggestionRequest(BaseModel):
    sources: List[SourceRef] = Field(..., min_length=1, max_length=5)


class EarlierCompose(BaseModel):
    id: str
    created_at: UtcDatetime
    output_type: Literal["article", "signal"]
    compose_type: Optional[str] = None
    desk: Optional[str] = None
    requested_by: Optional[str] = None
    article_id: Optional[str] = None
    title: Optional[str] = None


class SourceCheck(BaseModel):
    article: Optional[str] = Field(None, description="Why these sources cannot become a new article (empty = they can)")
    signal: Optional[str] = Field(None, description="Why these sources cannot become a new signal (empty = they can)")
    topics: Optional[str] = Field(None, description="Warning when the sources' topics share no desk (empty = no "
                                                    "warning); the reviewer may still go on")
    previous: List[EarlierCompose] = Field(default_factory=list, description="Finished composes of exactly these "
                                           "sources (last 30 days), newest first: a warning only")


class DeskSuggestion(BaseModel):
    desk: Optional[Desk] = None
    label: Optional[str] = None
    reason: Optional[str] = Field(None, description="The topics behind the suggestion")


class ExtractSignalRequest(BaseModel):
    article_id: str = Field(..., min_length=1, max_length=100)
    exclude_entity_ids: List[ItemId] = Field(default_factory=list, max_length=100)
    include_entity_ids: List[ItemId] = Field(default_factory=list, max_length=20)


class SignalSavedRequest(BaseModel):
    signal_id: str = Field(..., min_length=1, max_length=100)
    draft: int = Field(1, ge=1, le=3, description="Which of the run's drafts was saved (1-3)")


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
    compose_type: Optional[ComposeType] = None
    desk: Optional[Desk] = None
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
    saved: Optional[dict] = Field(None, description="Draft 1's save (kept for older screens); see saved_drafts")
    saved_drafts: Dict[str, dict] = Field(default_factory=dict, description='Saved drafts by number ("1"-"3")')
    source_rejected: bool = False
    source_rejected_key: Optional[str] = None
    pruned_at: Optional[UtcDatetime] = Field(None, description="When the job's details were removed (only its date, "
                                                                 "type, status, model, tokens and cost are kept)")


class ComposeRunList(BaseModel):
    items: List[ComposeRunOut]
    spent_today_usd: float
    daily_budget_usd: float
