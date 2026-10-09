"""
Newsroom knowledge-graph storage in the NEWSROOM_DB_NAME database.

NewsAgent is the only writer of these collections. Indexes are created here
(idempotently) because init_db.py is not run on every deploy.
"""
import logging
from dataclasses import dataclass
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure

from src.configs.settings import settings
from src.scheduler.db import get_async_mongo_client

logger = logging.getLogger("newsroom")

ENTITIES = "entities"
ENTITY_ALIASES = "entity_aliases"
ENTITY_RELATIONS = "entity_relations"
ENTITY_MENTIONS = "entity_mentions"
ENTITY_IGNORE = "entity_ignore"
ENTITY_AUDIT = "entity_audit"
ENTITY_SUGGESTIONS = "entity_suggestions"
COMPOSE_RUNS = "compose_runs"

# (collection, keys, options)
INDEX_SPECS = [
    (ENTITIES, [("kind", ASCENDING), ("canonical_norm", ASCENDING)], {"unique": True, "name": "kind_canonical_unique"}),
    (ENTITIES, [("kind", ASCENDING), ("status", ASCENDING), ("type", ASCENDING)], {"name": "kind_status_type"}),
    (ENTITIES, [("parent_id", ASCENDING)], {"name": "parent"}),
    (ENTITIES, [("path_ids", ASCENDING)], {"name": "path"}),
    (ENTITIES, [("context_slug", ASCENDING)], {"name": "context_slug", "sparse": True}),
    # An exact (non-ambiguous) alias belongs to one entity per kind; ambiguous aliases may repeat.
    (ENTITY_ALIASES, [("kind", ASCENDING), ("alias_norm", ASCENDING)],
     {"unique": True, "partialFilterExpression": {"ambiguous": False}, "name": "kind_alias_exact_unique"}),
    (ENTITY_ALIASES, [("entity_id", ASCENDING), ("alias_norm", ASCENDING)], {"unique": True, "name": "entity_alias_unique"}),
    (ENTITY_ALIASES, [("kind", ASCENDING), ("alias_norm", ASCENDING), ("ambiguous", ASCENDING)], {"name": "kind_alias_lookup"}),
    (ENTITY_RELATIONS, [("from_id", ASCENDING), ("to_id", ASCENDING), ("type", ASCENDING)], {"unique": True, "name": "relation_unique"}),
    (ENTITY_RELATIONS, [("to_id", ASCENDING), ("type", ASCENDING)], {"name": "relation_to"}),
    (ENTITY_MENTIONS, [("entity_id", ASCENDING), ("source_type", ASCENDING), ("source_id", ASCENDING)], {"unique": True, "name": "mention_unique"}),
    (ENTITY_MENTIONS, [("entity_id", ASCENDING), ("status", ASCENDING), ("timestamp", DESCENDING)], {"name": "entity_status_time"}),
    (ENTITY_MENTIONS, [("source_type", ASCENDING), ("source_id", ASCENDING), ("status", ASCENDING)], {"name": "source_status"}),
    (ENTITY_MENTIONS, [("entity_id", ASCENDING), ("location_ids", ASCENDING), ("timestamp", DESCENDING)], {"name": "entity_location_time"}),
    # The Newsroom list's place-only filter ("everything in Dubai")
    (ENTITY_MENTIONS, [("location_ids", ASCENDING), ("timestamp", DESCENDING)], {"name": "location_time"}),
    (ENTITY_MENTIONS, [("entity_id", ASCENDING), ("topics", ASCENDING), ("timestamp", DESCENDING)], {"name": "entity_topic_time"}),
    # One suggestion per item, field and name; dismissed ones stay so they are never shown again.
    (ENTITY_SUGGESTIONS, [("source_type", ASCENDING), ("source_id", ASCENDING), ("field", ASCENDING), ("name_norm", ASCENDING)],
     {"unique": True, "name": "suggestion_unique"}),
    (ENTITY_SUGGESTIONS, [("status", ASCENDING), ("in_new_names_queue", ASCENDING), ("field", ASCENDING), ("name_norm", ASCENDING)],
     {"name": "new_names"}),
    (ENTITY_SUGGESTIONS, [("entity_id", ASCENDING), ("status", ASCENDING)], {"name": "entity_status"}),
    (ENTITY_IGNORE, [("string_key", ASCENDING)], {"unique": True, "name": "ignore_unique"}),
    (ENTITY_AUDIT, [("entity_id", ASCENDING), ("at", DESCENDING)], {"name": "entity_time"}),
    (ENTITY_AUDIT, [("at", DESCENDING)], {"name": "time"}),
    (COMPOSE_RUNS, [("created_at", DESCENDING)], {"name": "created"}),
    (COMPOSE_RUNS, [("status", ASCENDING), ("deadline_at", ASCENDING)], {"name": "status_deadline"}),
    (COMPOSE_RUNS, [("source_keys", ASCENDING), ("status", ASCENDING)], {"name": "source_keys"}),
]


def get_newsroom_db():
    """FastAPI dependency: the async newsroom database (shares the scheduler's client)."""
    return get_async_mongo_client()[settings.NEWSROOM_DB_NAME]


@dataclass(frozen=True)
class ItemSources:
    """Where articles, signals and approved articles' stored fingerprints are read from (read-only)."""
    articles: Any
    signals: Any
    chunks: Any = None


def get_item_sources() -> ItemSources:
    """FastAPI dependency: NewsAgent's articles and social_scrapper's signals."""
    client = get_async_mongo_client()
    return ItemSources(
        articles=client[settings.MONGO_DB_NAME]["processed_articles"],
        signals=client[settings.NEWSROOM_DB_NAME][settings.NEWSROOM_SIGNALS_COLLECTION],
        chunks=client[settings.MONGO_DB_NAME][settings.MONGO_DB_COLLECTION],
    )


def ensure_indexes_sync(db) -> list:
    """Creates the Newsroom indexes on a sync pymongo database. Returns error messages; never raises."""
    errors = []
    for collection, keys, options in INDEX_SPECS:
        try:
            db[collection].create_index(keys, **options)
        except Exception as e:
            errors.append(f"{collection}.{options.get('name')}: {e}")
    return errors


async def ensure_indexes_async(db) -> list:
    """Async variant for service startup. Returns error messages; never raises."""
    errors = []
    for collection, keys, options in INDEX_SPECS:
        try:
            await db[collection].create_index(keys, **options)
        except ConnectionFailure as e:
            # Database unreachable: stop instead of waiting for a timeout on every index.
            errors.append(f"database unreachable: {e}")
            break
        except Exception as e:
            errors.append(f"{collection}.{options.get('name')}: {e}")
    return errors


async def ensure_newsroom_indexes() -> None:
    """Called at scheduler startup when the Newsroom is enabled. Logs problems and keeps the service running."""
    try:
        errors = await ensure_indexes_async(get_newsroom_db())
    except Exception as e:
        errors = [str(e)]
    for error in errors:
        logger.error(f"[NEWSROOM] Index creation failed: {error}")
    if not errors:
        logger.info("[NEWSROOM] Indexes ensured.")
