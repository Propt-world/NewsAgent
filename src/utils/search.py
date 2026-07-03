import time
from typing import Any, Dict, List, Optional, Tuple

import httpx


_CACHE: Dict[Tuple[str, str, int], Tuple[float, List[Dict[str, Any]]]] = {}


class SearxngSearchClient:
    """
    Thin SearXNG JSON API client used by graph nodes.

    The node-facing result shape stays stable for graph nodes:
    url, title, content, and query.
    """

    def __init__(
        self,
        base_url: str,
        engines: str,
        timeout_seconds: float,
        max_results: int,
        cache_ttl_seconds: int,
        language: str,
        safesearch: int,
    ):
        self.base_url = base_url.rstrip("/")
        self.engines = engines
        self.timeout_seconds = timeout_seconds
        self.max_results = max_results
        self.cache_ttl_seconds = cache_ttl_seconds
        self.language = language
        self.safesearch = safesearch

    def search(self, query: str, max_results: Optional[int] = None) -> List[Dict[str, Any]]:
        limit = max_results or self.max_results
        cache_key = (query.strip().lower(), self.engines, limit)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(
                f"{self.base_url}/search",
                params=self._request_params(query),
                headers=self._request_headers(),
            )
            response.raise_for_status()
            payload = response.json()

        normalized = self._normalize_payload(payload, query, limit)
        self._set_cached(cache_key, normalized)
        return normalized

    async def search_async(
        self,
        query: str,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        limit = max_results or self.max_results
        cache_key = (query.strip().lower(), self.engines, limit)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"{self.base_url}/search",
                params=self._request_params(query),
                headers=self._request_headers(),
            )
            response.raise_for_status()
            payload = response.json()

        normalized = self._normalize_payload(payload, query, limit)
        self._set_cached(cache_key, normalized)
        return normalized

    def _request_params(self, query: str) -> Dict[str, Any]:
        return {
            "q": query,
            "format": "json",
            "engines": self.engines,
            "safesearch": self.safesearch,
            "language": self.language,
        }

    @staticmethod
    def _request_headers() -> Dict[str, str]:
        return {
            "User-Agent": "NewsAgent/1.0",
            "X-Real-IP": "127.0.0.1",
        }

    def _normalize_payload(
        self,
        payload: Dict[str, Any],
        query: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        results = [
            self._normalize_result(result, query)
            for result in payload.get("results", [])
            if result.get("url")
        ]
        return results[:limit]

    def _get_cached(self, cache_key: Tuple[str, str, int]) -> Optional[List[Dict[str, Any]]]:
        if self.cache_ttl_seconds <= 0:
            return None

        cached = _CACHE.get(cache_key)
        if not cached:
            return None

        expires_at, results = cached
        if expires_at < time.time():
            _CACHE.pop(cache_key, None)
            return None

        return results

    def _set_cached(self, cache_key: Tuple[str, str, int], results: List[Dict[str, Any]]) -> None:
        if self.cache_ttl_seconds <= 0:
            return

        _CACHE[cache_key] = (time.time() + self.cache_ttl_seconds, results)

    @staticmethod
    def _normalize_result(result: Dict[str, Any], query: str) -> Dict[str, Any]:
        engines = result.get("engines") or []
        engine = result.get("engine")
        if not engine and engines:
            engine = engines[0]

        return {
            "title": result.get("title") or "",
            "url": result.get("url"),
            "content": result.get("content") or "",
            "source": engine,
            "score": result.get("score"),
            "published_date": (
                result.get("publishedDate")
                or result.get("published_date")
                or result.get("published")
            ),
            "query": query,
        }
