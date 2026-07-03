import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any

import certifi
from pymongo import AsyncMongoClient, MongoClient

from src.configs.settings import settings


logger = logging.getLogger(__name__)

_async_client = None
_legacy_sync_client = None


def _database_target() -> str:
    if "@" in settings.DATABASE_URL:
        return settings.DATABASE_URL.split("@")[-1]
    return settings.DATABASE_URL


def _mongo_client_options() -> dict:
    options = {
        "connectTimeoutMS": int(os.getenv("MONGO_CONNECT_TIMEOUT_MS", "5000")),
        "socketTimeoutMS": int(os.getenv("MONGO_SOCKET_TIMEOUT_MS", "5000")),
        "serverSelectionTimeoutMS": int(
            os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000")
        ),
        "retryWrites": True,
        "retryReads": True,
        "maxPoolSize": int(os.getenv("MONGO_MAX_POOL_SIZE", "100")),
        "minPoolSize": int(os.getenv("MONGO_MIN_POOL_SIZE", "0")),
        "maxIdleTimeMS": int(os.getenv("MONGO_MAX_IDLE_TIME_MS", "50000")),
        "waitQueueTimeoutMS": int(os.getenv("MONGO_WAIT_QUEUE_TIMEOUT_MS", "2000")),
    }

    if "mongodb.net" in settings.DATABASE_URL or settings.DATABASE_URL.startswith(
        "mongodb+srv://"
    ):
        options["tls"] = True
        options["tlsCAFile"] = certifi.where()

    return options


def get_async_mongo_client():
    global _async_client

    if _async_client is None:
        logger.info(f"Initializing async scheduler MongoDB client: {_database_target()}")
        _async_client = AsyncMongoClient(settings.DATABASE_URL, **_mongo_client_options())

    return _async_client


def get_async_database():
    return get_async_mongo_client()[settings.MONGO_DB_NAME]


def get_async_collection(name: str):
    return get_async_database()[name]


async def ping_async_database() -> None:
    await get_async_mongo_client().admin.command("ping")


async def close_async_mongo_client() -> None:
    global _async_client

    if _async_client is None:
        return

    result = _async_client.close()
    if inspect.isawaitable(result):
        await result
    _async_client = None


def get_legacy_mongo_client():
    global _legacy_sync_client

    if _legacy_sync_client is None:
        logger.info(f"Initializing legacy scheduler MongoDB client: {_database_target()}")
        _legacy_sync_client = MongoClient(
            settings.DATABASE_URL,
            **_mongo_client_options(),
        )

    return _legacy_sync_client


def get_legacy_database():
    return get_legacy_mongo_client()[settings.MONGO_DB_NAME]


def get_legacy_collection(name: str):
    return get_legacy_database()[name]


def ping_legacy_database() -> None:
    get_legacy_mongo_client().admin.command("ping")


def close_legacy_mongo_client() -> None:
    global _legacy_sync_client

    if _legacy_sync_client is None:
        return

    _legacy_sync_client.close()
    _legacy_sync_client = None


class LegacyMongoCollectionProxy:
    """
    Lazy proxy for existing sync scheduler code.

    Keep this around during the migration so legacy code can continue to use
    collection methods like find_one/update_one while new code moves to
    get_async_collection().
    """

    def __init__(self, name: str):
        self.name = name

    @property
    def collection(self):
        return get_legacy_collection(self.name)

    def __getattr__(self, attr: str):
        return getattr(self.collection, attr)


@dataclass(frozen=True)
class SchedulerCollections:
    sources: Any
    processed_articles: Any
    archived_articles: Any
    deleted_articles: Any


def get_async_scheduler_collections() -> SchedulerCollections:
    return SchedulerCollections(
        sources=get_async_collection("sources"),
        processed_articles=get_async_collection("processed_articles"),
        archived_articles=get_async_collection("archived_articles"),
        deleted_articles=get_async_collection("deleted_articles"),
    )


def get_legacy_scheduler_collections() -> SchedulerCollections:
    return SchedulerCollections(
        sources=get_legacy_collection("sources"),
        processed_articles=get_legacy_collection("processed_articles"),
        archived_articles=get_legacy_collection("archived_articles"),
        deleted_articles=get_legacy_collection("deleted_articles"),
    )


legacy_sources_col = LegacyMongoCollectionProxy("sources")
legacy_articles_col = LegacyMongoCollectionProxy("processed_articles")
legacy_archive_col = LegacyMongoCollectionProxy("archived_articles")
legacy_deleted_col = LegacyMongoCollectionProxy("deleted_articles")
