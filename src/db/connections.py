import inspect
import logging
import os
from typing import Optional

import certifi
import redis
import redis.asyncio as async_redis
from pymongo import AsyncMongoClient, MongoClient

from src.configs.settings import settings


logger = logging.getLogger(__name__)

_async_mongo_client = None
_legacy_mongo_client = None
_async_redis_client: Optional[async_redis.Redis] = None
_legacy_redis_pool = None


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
    global _async_mongo_client

    if _async_mongo_client is None:
        logger.info("Initializing async API MongoDB client: %s", _database_target())
        _async_mongo_client = AsyncMongoClient(
            settings.DATABASE_URL,
            **_mongo_client_options(),
        )

    return _async_mongo_client


def get_async_mongo_database():
    return get_async_mongo_client()[settings.MONGO_DB_NAME]


def get_async_mongo_collection(name: str):
    return get_async_mongo_database()[name]


async def ping_async_mongo() -> None:
    await get_async_mongo_client().admin.command("ping")


async def close_async_mongo_client() -> None:
    global _async_mongo_client

    if _async_mongo_client is None:
        return

    result = _async_mongo_client.close()
    if inspect.isawaitable(result):
        await result
    _async_mongo_client = None


def get_legacy_mongo_client():
    global _legacy_mongo_client

    if _legacy_mongo_client is None:
        logger.info("Initializing legacy API MongoDB client: %s", _database_target())
        _legacy_mongo_client = MongoClient(
            settings.DATABASE_URL,
            **_mongo_client_options(),
        )

    return _legacy_mongo_client


def get_legacy_mongo_database():
    return get_legacy_mongo_client()[settings.MONGO_DB_NAME]


def get_legacy_mongo_collection(name: str):
    return get_legacy_mongo_database()[name]


def close_legacy_mongo_client() -> None:
    global _legacy_mongo_client

    if _legacy_mongo_client is None:
        return

    _legacy_mongo_client.close()
    _legacy_mongo_client = None


def get_async_redis_client() -> async_redis.Redis:
    global _async_redis_client

    if _async_redis_client is None:
        logger.info("Initializing async API Redis client.")
        _async_redis_client = async_redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,
        )

    return _async_redis_client


async def close_async_redis_client() -> None:
    global _async_redis_client

    if _async_redis_client is None:
        return

    await _async_redis_client.aclose()
    _async_redis_client = None


def get_legacy_redis_client() -> redis.Redis:
    global _legacy_redis_pool

    if _legacy_redis_pool is None:
        logger.info("Initializing legacy API Redis pool.")
        _legacy_redis_pool = redis.ConnectionPool.from_url(settings.REDIS_URL)

    return redis.Redis(connection_pool=_legacy_redis_pool)


async def close_async_api_connections() -> None:
    await close_async_redis_client()
    await close_async_mongo_client()


def close_legacy_api_connections() -> None:
    global _legacy_redis_pool

    close_legacy_mongo_client()
    if _legacy_redis_pool is not None:
        _legacy_redis_pool.disconnect()
        _legacy_redis_pool = None
