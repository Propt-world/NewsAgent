import time
import redis
import logging
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from pymongo import MongoClient

from src.configs.settings import settings

logger = logging.getLogger("governance")

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
            rp.set_url(robots_url)
            rp.read()
            is_allowed = rp.can_fetch(self.user_agent, url)
        except Exception:
            is_allowed = True # Default to allow if robots.txt fails

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