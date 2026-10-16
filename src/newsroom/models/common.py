"""Shared building blocks for the Newsroom API models: UTC dates, size limits, kinds and statuses."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, StringConstraints


def _as_utc(value: datetime) -> datetime:
    # MongoDB returns UTC times without a timezone; mark them as UTC so browsers convert them correctly.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]

# Size limits on what a request may send: ids (uuids, ObjectIds, article keys) and company/place names.
ItemId = Annotated[str, StringConstraints(min_length=1, max_length=100)]
NameText = Annotated[str, StringConstraints(max_length=200)]

EntityKind = Literal["organisation", "location"]
# "merged" is set only by a merge; reviewers choose between the other three.
EntityStatus = Literal["active", "staged", "ignored", "merged"]
EditableStatus = Literal["active", "staged", "ignored"]

SourceType = Literal["article", "signal"]
SuggestionField = Literal["organisation", "location"]
