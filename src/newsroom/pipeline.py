"""
Hooks between the existing article pipeline and the Newsroom.

Only used when NEWSROOM_ENABLED is true. Every hook catches its own errors so
article processing and review never fail because of the Newsroom.
"""
import logging
from typing import Any, List, Tuple

from src.newsroom import article_vectors
from src.newsroom import links as lnk
from src.newsroom.compose import COMPOSED_SOURCE_ID, composed_url
from src.newsroom.desks import is_newsroom_byline
from src.newsroom.db import get_item_sources, get_newsroom_db
from src.scheduler.db import get_async_scheduler_collections

logger = logging.getLogger("newsroom")
COMPOSED_URL_PREFIX = composed_url("")

MAX_NAMES_PER_FIELD = 50
MAX_NAME_CHARS = 200


def _strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    names = [v.strip()[:MAX_NAME_CHARS] for v in value if isinstance(v, str) and v.strip()]
    return names[:MAX_NAMES_PER_FIELD]


def split_names(newsroom_names: Any) -> Tuple[List[str], List[str]]:
    """The pipeline's companies + projects are organisation names; places are location names."""
    if not isinstance(newsroom_names, dict):
        return [], []
    organisations = _strings(newsroom_names.get("companies")) + _strings(newsroom_names.get("projects"))
    return organisations, _strings(newsroom_names.get("places"))


def newsroom_composed(url: str, newsroom: Any) -> bool:
    """True for a well-formed composed-article result (Propt Newsroom) sent by the compose worker."""
    return (
        isinstance(newsroom, dict)
        and url.startswith(COMPOSED_URL_PREFIX)
        and newsroom.get("generated_from") == url[len(COMPOSED_URL_PREFIX):]
        and newsroom.get("source_id") == COMPOSED_SOURCE_ID
        # "Propt Newsroom", or with a desk: "Propt Newsroom · Rental Desk"
        and is_newsroom_byline(newsroom.get("author"))
    )


async def article_stored(url: str, newsroom_names: Any) -> None:
    """
    Background step after the result webhook stored an article: creates entity suggestions from the
    names the pipeline found, and refreshes existing links (an article can be processed again).
    """
    article = None
    try:
        article = await get_async_scheduler_collections().processed_articles.find_one({"url": url}, {"_id": 1})
        if article is None:
            logger.warning(f"[NEWSROOM] No article for {url}; suggestions skipped.")
            return
        organisations, locations = split_names(newsroom_names)
        if organisations or locations:
            counts = await lnk.suggest(get_newsroom_db(), "article", article["_id"], organisations, locations, "pipeline")
            logger.info(f"[NEWSROOM] Suggestions for article {article['_id']}: {counts}")
        await lnk.item_changed(get_newsroom_db(), get_item_sources(), "article", article["_id"])
    except Exception:
        logger.exception(f"[NEWSROOM] Article hook failed for {url}")
        if article is None:
            return
    # A small vector while it waits in Pending, for text matches (never raises)
    await article_vectors.refresh(get_newsroom_db(), get_item_sources().articles, article["_id"])


async def article_edited(article_id: str) -> None:
    """After an article's title or text is edited: its links show the new title, and a pending article's vector
    follows the new title and summary."""
    try:
        await lnk.item_changed(get_newsroom_db(), get_item_sources(), "article", article_id)
    except Exception:
        logger.exception(f"[NEWSROOM] Edit hook failed for article {article_id}")
    await article_vectors.refresh(get_newsroom_db(), get_item_sources().articles, article_id)


async def article_moved(article_id: str) -> None:
    """After the Archive or Delete button moved an article: archived stays linked, deleted is hidden; either way it
    is no longer pending, so its pending vector goes."""
    try:
        await lnk.item_changed(get_newsroom_db(), get_item_sources(), "article", article_id)
    except Exception:
        logger.exception(f"[NEWSROOM] Archive/delete hook failed for article {article_id}")
    await article_vectors.drop(get_newsroom_db(), article_id)


async def article_status_changed(article_id: str, new_status: str) -> None:
    """Keeps links' item status current and feeds the new-names queue on approval. The pending vector goes when the
    article leaves Pending (approval makes the full vectors as before) and comes back if it is set to Pending again."""
    try:
        await lnk.on_item_status(get_newsroom_db(), "article", article_id, new_status)
    except Exception:
        logger.exception(f"[NEWSROOM] Status hook failed for article {article_id}")
    await article_vectors.refresh(get_newsroom_db(), get_item_sources().articles, article_id)
