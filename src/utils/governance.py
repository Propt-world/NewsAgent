import asyncio
import time
import redis
import redis.asyncio as aioredis
import logging
import requests
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
import certifi
import httpx
from pymongo import AsyncMongoClient, MongoClient

from src.configs.settings import settings

logger = logging.getLogger("governance")

_async_redis_client = None
_async_mongo_client = None


def _mongo_client_options() -> dict:
    options = {
        "connectTimeoutMS": 5000,
        "socketTimeoutMS": 5000,
        "serverSelectionTimeoutMS": 5000,
        "retryWrites": True,
        "retryReads": True,
        "maxPoolSize": 100,
        "minPoolSize": 0,
        "maxIdleTimeMS": 50000,
        "waitQueueTimeoutMS": 2000,
    }

    if "mongodb.net" in settings.DATABASE_URL or settings.DATABASE_URL.startswith(
        "mongodb+srv://"
    ):
        options["tls"] = True
        options["tlsCAFile"] = certifi.where()

    return options


def get_async_governance_redis():
    global _async_redis_client

    if _async_redis_client is None:
        _async_redis_client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )

    return _async_redis_client


def get_async_governance_database():
    global _async_mongo_client

    if _async_mongo_client is None:
        _async_mongo_client = AsyncMongoClient(
            settings.DATABASE_URL,
            **_mongo_client_options(),
        )

    return _async_mongo_client[settings.MONGO_DB_NAME]


async def close_async_governance_clients() -> None:
    global _async_redis_client, _async_mongo_client

    if _async_redis_client is not None:
        await _async_redis_client.aclose()
        _async_redis_client = None

    if _async_mongo_client is not None:
        result = _async_mongo_client.close()
        if asyncio.iscoroutine(result):
            await result
        _async_mongo_client = None


class GovernanceGatekeeper:
    def __init__(self):
        # 1. Redis Connection (for Rate Locking & Caching)
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        
        # 2. Mongo Connection (for fetching dynamic Config)
        self.mongo_client = MongoClient(settings.DATABASE_URL)
        self.db = self.mongo_client[settings.MONGO_DB_NAME]
        
        # 3. Bot Identity
        self.user_agent = settings.USER_AGENT 

    def _get_domain(self, url: str) -> str:
        return urlparse(url).netloc

    def _get_config_cache_key(self, domain: str) -> str:
        return f"config:delay:{domain}"

    def _get_dynamic_delay(self, domain: str, default_delay: int = 5) -> int:
        """
        Fetches the delay setting for a domain.
        Strategy: Redis Cache -> MongoDB Lookup -> Default
        """
        cache_key = self._get_config_cache_key(domain)

        # A. Check Redis Cache (Fastest)
        cached_val = self.redis.get(cache_key)
        if cached_val:
            return int(cached_val)

        # B. Check MongoDB (If not in cache)
        # We look for a source that has this domain in its listing_url
        try:
            source_doc = self.db.sources.find_one(
                {"listing_url": {"$regex": domain}},
                {"delay_seconds": 1} # Projection: only fetch this field
            )
            
            delay = default_delay
            if source_doc and "delay_seconds" in source_doc:
                delay = int(source_doc["delay_seconds"])

            # C. Cache the result for 5 minutes (300s)
            # This allows you to change DB and see effect in <5 mins
            # without hammering Mongo on every request.
            self.redis.setex(cache_key, 300, delay)
            
            return delay

        except Exception as e:
            logger.error(f"Error fetching delay config for {domain}: {e}")
            return default_delay

    def can_fetch(self, url: str) -> bool:
        """
        Checks robots.txt compliance.
        """
        domain = self._get_domain(url)
        robots_key = f"robots_cache:{domain}"

        # 1. Check Cache
        cached_status = self.redis.get(robots_key)
        if cached_status is not None:
            return cached_status == "1"

        # 2. Check Live
        robots_url = f"{urlparse(url).scheme}://{domain}/robots.txt"
        rp = RobotFileParser()
        try:
            # We manually fetch robots.txt because rp.read() uses a default UA 
            # that is often blocked (e.g., by Gulf News).
            headers = {"User-Agent": self.user_agent}
            response = requests.get(robots_url, headers=headers, timeout=10)
            
            # If we get a 403, it's often because they block even browser-like UAs 
            # if they suspect it's a bot, or the UA isn't "good" enough.
            # But the spec says 403 = "Disallow All".
            # If we get 404, it means "Allow All".
            if response.status_code == 403:
                is_allowed = False
            elif response.status_code == 404:
                is_allowed = True
            else:
                response.raise_for_status()
                rp.parse(response.text.splitlines())
                is_allowed = rp.can_fetch(self.user_agent, url)
        except Exception as e:
            logger.warning(f"Error fetching robots.txt for {domain}: {e}. Defaulting to ALLOW.")
            is_allowed = True # Default to allow if robots.txt fetch fails (unless it was a 403)

        # 3. Cache (24 hours)
        self.redis.setex(robots_key, 86400, "1" if is_allowed else "0")
        return is_allowed

    def wait_for_slot(self, url: str) -> None:
        """
        BLOCKING: Distributed Rate Limiting with Dynamic Configuration.
        """
        domain = self._get_domain(url)
        
        # 1. Get the delay dynamically (Default 5s if not configured)
        delay_seconds = self._get_dynamic_delay(domain, default_delay=5)
        
        lock_key = f"rate_limit:{domain}"

        while True:
            # Try to acquire lock
            is_acquired = self.redis.set(
                lock_key, 
                "locked", 
                nx=True, 
                px=delay_seconds * 1000 # TTL in milliseconds
            )

            if is_acquired:
                logger.info(f"🟢 Rate Limit Acquired for {domain} (Delay: {delay_seconds}s)")
                return
            else:
                # Wait based on remaining TTL
                ttl = self.redis.pttl(lock_key)
                if ttl > 0:
                    sleep_time = (ttl / 1000.0) + 0.1
                    time.sleep(sleep_time)
                else:
                    time.sleep(1)


class AsyncGovernanceGatekeeper:
    def __init__(self):
        self.redis = get_async_governance_redis()
        self.db = get_async_governance_database()
        self.user_agent = settings.USER_AGENT

    def _get_domain(self, url: str) -> str:
        return urlparse(url).netloc

    def _get_config_cache_key(self, domain: str) -> str:
        return f"config:delay:{domain}"

    async def _get_dynamic_delay(self, domain: str, default_delay: int = 5) -> int:
        """
        Fetches the delay setting for a domain.
        Strategy: Redis Cache -> MongoDB Lookup -> Default
        """
        cache_key = self._get_config_cache_key(domain)

        cached_val = await self.redis.get(cache_key)
        if cached_val:
            try:
                return int(cached_val)
            except ValueError:
                logger.warning(f"Invalid cached delay for {domain}: {cached_val}")

        try:
            source_doc = await self.db.sources.find_one(
                {"listing_url": {"$regex": domain}},
                {"delay_seconds": 1},
            )

            delay = default_delay
            if source_doc and "delay_seconds" in source_doc:
                delay = int(source_doc["delay_seconds"])

            await self.redis.setex(cache_key, 300, delay)
            return delay

        except Exception as e:
            logger.error(f"Error fetching delay config for {domain}: {e}")
            return default_delay

    async def can_fetch(self, url: str) -> bool:
        """
        Checks robots.txt compliance without blocking the event loop.
        """
        domain = self._get_domain(url)
        robots_key = f"robots_cache:{domain}"

        cached_status = await self.redis.get(robots_key)
        if cached_status is not None:
            return cached_status == "1"

        robots_url = f"{urlparse(url).scheme}://{domain}/robots.txt"
        rp = RobotFileParser()
        try:
            headers = {"User-Agent": self.user_agent}
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(robots_url, headers=headers)

            if response.status_code == 403:
                is_allowed = False
            elif response.status_code == 404:
                is_allowed = True
            else:
                response.raise_for_status()
                rp.parse(response.text.splitlines())
                is_allowed = rp.can_fetch(self.user_agent, url)
        except Exception as e:
            logger.warning(
                f"Error fetching robots.txt for {domain}: {e}. Defaulting to ALLOW."
            )
            is_allowed = True

        await self.redis.setex(robots_key, 86400, "1" if is_allowed else "0")
        return is_allowed

    async def wait_for_slot(self, url: str) -> None:
        """
        Distributed rate limiting with dynamic configuration.
        """
        domain = self._get_domain(url)
        delay_seconds = await self._get_dynamic_delay(domain, default_delay=5)
        lock_key = f"rate_limit:{domain}"

        while True:
            is_acquired = await self.redis.set(
                lock_key,
                "locked",
                nx=True,
                px=delay_seconds * 1000,
            )

            if is_acquired:
                logger.info(
                    f"Rate limit acquired for {domain} (Delay: {delay_seconds}s)"
                )
                return

            ttl = await self.redis.pttl(lock_key)
            if ttl > 0:
                await asyncio.sleep((ttl / 1000.0) + 0.1)
            else:
                await asyncio.sleep(1)
