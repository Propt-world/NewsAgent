"""
Newsroom API models, split by area (user decision 2026-10-16):
common (shared types), status, entities, links, related, compose.
Everything is re-exported here, so `from src.newsroom.models import X` works for every model.
"""
from src.newsroom.models.common import (  # noqa: F401
    UtcDatetime,
    ItemId,
    NameText,
    EntityKind,
    EntityStatus,
    EditableStatus,
    SourceType,
    SuggestionField,
)
from src.newsroom.models.status import (  # noqa: F401
    NewsroomHealthResponse,
    StatusProblem,
    NewsroomStatusResponse,
)
from src.newsroom.models.entities import (  # noqa: F401
    AliasOut,
    EntityOut,
    EntityListResponse,
    EntityCreate,
    EntityUpdate,
    AliasCreate,
    MergeRequest,
    MergeResult,
    DeleteRequest,
    DeleteResult,
    CsvExport,
    CsvTemplate,
    CsvImportRequest,
    CsvImportRow,
    CsvImportResult,
    IgnoreOut,
    IgnoreCreate,
    AuditOut,
)
from src.newsroom.models.links import (  # noqa: F401
    SuggestRequest,
    SuggestResponse,
    SuggestionOut,
    LinkOut,
    ItemEntitiesResponse,
    LinkRequest,
    NewNameItem,
    NewNameGroup,
    NewNameLinkRequest,
    NewNameDismissRequest,
    CountResponse,
    ItemsSummaryRequest,
    ItemSummary,
    ItemsSummaryResponse,
    ItemsChangedRequest,
    ItemsChangedResponse,
)
from src.newsroom.models.related import (  # noqa: F401
    EntityRef,
    RelatedItem,
    RelatedResponse,
    HistoryEntity,
    HistoryItem,
    HistoryResponse,
    FilteredItemsResponse,
)
from src.newsroom.models.compose import (  # noqa: F401
    SourceRef,
    ComposeType,
    Desk,
    ComposeRequest,
    DeskSuggestionRequest,
    SourceCheck,
    DeskSuggestion,
    ExtractSignalRequest,
    SignalSavedRequest,
    ComposeSourceOut,
    ComposeRunOut,
    ComposeRunList,
)
