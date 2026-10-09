"""
The matching rules for one name, in one place, used by live suggestions and by
the backfill trial so the two can never disagree.

Order: website/handle -> ignore list (original text, before normalising) ->
aliases of the normalised name (an exact alias wins; only ambiguous ones give
nothing) -> the entity's status.
"""
from dataclasses import dataclass
from typing import Iterable, List, Optional

from src.newsroom.normalize import looks_like_handle, raw_key

HANDLE = "handle"              # looks like a website or social account
IGNORED = "ignored"            # on the ignore list
AMBIGUOUS = "ambiguous"        # the name only matches ambiguous aliases
UNKNOWN = "unknown"            # not in the list
SWITCHED_OFF = "switched_off"  # the entity is missing or has status "ignored"
STAGED = "staged"              # exact match on an entity that is not switched on yet
ACTIVE = "active"              # exact match on a switched-on entity


@dataclass
class Resolution:
    outcome: str
    alias: Optional[dict] = None
    entity: Optional[dict] = None


def precheck(name: str, ignored_keys: Iterable[str]) -> Optional[str]:
    """HANDLE or IGNORED when the name must be skipped before any lookup, else None."""
    if looks_like_handle(name):
        return HANDLE
    if raw_key(name) in ignored_keys:
        return IGNORED
    return None


def exact_alias(aliases: List[dict]) -> Optional[dict]:
    return next((a for a in aliases if not a.get("ambiguous")), None)


def resolve(name: str, ignored_keys: Iterable[str], aliases: List[dict], entity: Optional[dict]) -> Resolution:
    """
    aliases: every alias of the same kind whose alias_norm equals the name's normalised form.
    entity: the entity owning exact_alias(aliases), or None.
    """
    skipped = precheck(name, ignored_keys)
    if skipped:
        return Resolution(skipped)
    exact = exact_alias(aliases)
    if exact is None:
        return Resolution(AMBIGUOUS if aliases else UNKNOWN)
    if entity is None or entity.get("status") == "ignored":
        return Resolution(SWITCHED_OFF, exact, entity)
    return Resolution(ACTIVE if entity.get("status") == "active" else STAGED, exact, entity)
