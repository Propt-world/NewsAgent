"""
Who changed an article, when, and what it was before; and protection against two reviewers overwriting each
other's edits (user decisions 2026-10-23).

- record(): one entry per change in `article_history` (title, text, image or status) with the values before
  and after. The reviewer is the X-Newsroom-User header the backoffice sends (informational, not a security
  check; see the post-build security list).
- Text and title edits carry the article's `version` they started from (expected_version). The save is refused
  (409) when the article was edited since, naming who edited it and when; each edit adds 1 to `version`.
  A request without expected_version is not checked (older screens).
- Status changes also store status_changed_at / status_changed_by on the article (the dashboard counts use them).
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Header

from src.scheduler.db import get_async_collection

HISTORY = "article_history"
HISTORY_LIMIT = 50


class EditConflict(Exception):
    """The article was edited after the reviewer opened it."""


def editor(x_newsroom_user: Optional[str] = Header(None)) -> str:
    """The reviewer, as the backoffice names them (X-Newsroom-User)."""
    return (x_newsroom_user or "").strip()[:200] or "unknown"


def now() -> datetime:
    return datetime.now(timezone.utc)


def version_query(article_id: str, expected_version: Optional[int]) -> Dict[str, Any]:
    """The update filter: the article, and (when the screen sent one) still at the version it was opened at.
    Articles from before versions count as version 0."""
    if expected_version is None:
        return {"_id": article_id}
    if expected_version == 0:
        return {"_id": article_id, "$or": [{"version": 0}, {"version": {"$exists": False}}]}
    return {"_id": article_id, "version": expected_version}


def edit_stamp(by: str) -> Dict[str, Any]:
    """$set and $inc parts every text or title edit adds."""
    return {"$set": {"edited_at": now(), "edited_by": by}, "$inc": {"version": 1}}


def conflict_message(doc: Optional[dict]) -> str:
    who = (doc or {}).get("edited_by") or "someone"
    when = (doc or {}).get("edited_at")
    at = f" at {when.strftime('%d %b %Y %H:%M')} UTC" if isinstance(when, datetime) else ""
    return f"This article was changed by {who}{at} after you opened it. Load the latest version, then make your change again."


async def check_version(articles, article_id: str, expected_version: Optional[int]) -> None:
    """Refuses early (before any AI step) when the article was edited since it was opened."""
    if expected_version is None:
        return
    doc = await articles.find_one({"_id": article_id}, {"version": 1, "edited_by": 1, "edited_at": 1})
    if doc is not None and (doc.get("version") or 0) != expected_version:
        raise EditConflict(conflict_message(doc))


async def record(article_id: str, by: str, action: str, before: Dict[str, Any], after: Dict[str, Any]) -> None:
    """One history entry: action is "title", "text", "image" or "status"."""
    await get_async_collection(HISTORY).insert_one(
        {"article_id": article_id, "at": now(), "by": by, "action": action, "before": before, "after": after})


async def list_history(article_id: str, limit: int = HISTORY_LIMIT) -> List[dict]:
    """The article's changes, newest first."""
    docs = await get_async_collection(HISTORY).find({"article_id": article_id}).sort("at", -1).limit(limit).to_list(length=limit)
    return [{**{k: v for k, v in d.items() if k != "_id"}, "id": str(d["_id"])} for d in docs]
