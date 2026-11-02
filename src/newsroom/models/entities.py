"""The entity manager: entities and their names, merge, delete, CSV export/import, the ignore list, history."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from src.newsroom.models.common import EditableStatus, EntityKind, EntityStatus, ItemId, NameText, UtcDatetime


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
    parent_id: Optional[ItemId] = None
    country_id: Optional[ItemId] = None
    notes: Optional[str] = Field(None, max_length=2000)
    aliases: List[NameText] = Field(default_factory=list, max_length=50)
    ambiguous_aliases: List[NameText] = Field(default_factory=list, max_length=50)


class EntityUpdate(BaseModel):
    """Only the fields that are sent are changed; send parent_id/country_id as null to clear them."""
    type: Optional[str] = Field(None, min_length=1, max_length=64)
    canonical_name: Optional[str] = Field(None, min_length=1, max_length=200)
    status: Optional[EditableStatus] = None
    parent_id: Optional[ItemId] = None
    country_id: Optional[ItemId] = None
    notes: Optional[str] = Field(None, max_length=2000)


class AliasCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=200)
    ambiguous: bool = False


class MergeRequest(BaseModel):
    into_id: ItemId = Field(..., description="The entity that is kept")
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
    confirm: Optional[str] = Field(None, max_length=50, description='Must be "delete" (any case) unless dry_run')
    dry_run: bool = Field(False, description="Only count what would be removed")


class DeleteResult(BaseModel):
    dry_run: bool
    entity_id: str
    canonical_name: str
    aliases_removed: int
    links_removed: int = Field(..., description="Links to articles and signals")
    decisions_removed: int = Field(..., description="Reviewer removals remembered for this entry")
    suggestions_reset: int = Field(..., description="Open suggestions that go back to 'not in the list'")


class CsvExport(BaseModel):
    filename: str
    csv: str


class CsvTemplate(BaseModel):
    filename: str
    csv: str
    columns: List[str]
    types: List[str] = Field(..., description="Accepted values for the type column")


class CsvImportRequest(BaseModel):
    kind: EntityKind
    csv_text: str = Field(..., max_length=1_000_000, description="The file's text (up to about 1 MB)")
    dry_run: bool = Field(True, description="Only check the rows; nothing is added")


class CsvImportRow(BaseModel):
    row: int = Field(..., description="Line number in the file (the column names are line 1)")
    name: str
    outcome: Literal["add", "added", "skip", "error"]
    message: str = ""


class CsvImportResult(BaseModel):
    dry_run: bool
    kind: EntityKind
    rows: List[CsvImportRow]
    to_add: int
    added: int
    skipped: int
    problems: int


class IgnoreOut(BaseModel):
    id: str
    string: str
    reason: Optional[str] = None
    created_at: Optional[UtcDatetime] = None
    created_by: Optional[str] = None


class IgnoreCreate(BaseModel):
    string: str = Field(..., min_length=1, max_length=200)
    reason: Optional[str] = Field(None, max_length=500)


class AuditOut(BaseModel):
    id: str
    at: UtcDatetime
    actor: str
    action: str
    entity_id: Optional[str] = None
    before: Optional[dict] = None
    after: Optional[dict] = None
