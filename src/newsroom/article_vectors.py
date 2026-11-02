"""
A small vector for each article waiting for review (Pending), so the Newsroom can find approved articles and signals
with the same meaning when a pending article is opened (approved articles' vectors are only made at approval).

- Made from the title and summary, with the same model as the approved articles' and signals' vectors
  (MONGO_EMBEDDING_MODEL, text-embedding-3-large), so they can be compared; about $0.00004 per article.
- Stored in its own collection (article_vectors, one document per article id), packed (float32, about 12 KB), so
  articles, lists and the chatbot are unchanged. No Atlas index: it is only read by article id, to compare with.
- Kept only while the article is pending: deleted when it is approved (the approval step makes the full vectors as
  before), rejected, marked duplicated, archived or deleted. Made again only when the title or summary changed.
- Never raises: without a vector the article simply gets no text matches.
"""
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson.binary import Binary, BinaryVectorDtype

from src.configs.settings import settings
from src.newsroom import db as ndb
from src.utils import tracing

logger = logging.getLogger("newsroom")

PENDING = "processed"  # the status the Pending tab lists


def vector_text(final_output: Optional[Dict[str, Any]]) -> str:
    final = final_output or {}
    title, summary = (final.get("title") or "").strip(), (final.get("summary") or "").strip()
    return f"{title}\n{summary}".strip()


def _text_hash(text: str) -> str:
    return hashlib.sha256(f"{settings.MONGO_EMBEDDING_MODEL}\n{text}".encode("utf-8")).hexdigest()


def pack(vector: List[float]) -> Binary:
    return Binary.from_vector(vector, BinaryVectorDtype.FLOAT32)


def unpack(stored: Any) -> List[float]:
    return list(stored.as_vector().data) if isinstance(stored, Binary) else list(stored or [])


async def make_vector(text: str) -> List[float]:
    """One embedding call (traced in Phoenix with its tokens)."""
    from src.utils.mongo_store import get_mongo_store

    return list(await get_mongo_store().get_embedding(text))


async def get(db, article_id: str) -> Optional[List[float]]:
    """The pending article's vector, or None."""
    doc = await db[ndb.ARTICLE_VECTORS].find_one({"_id": article_id}, {"vector": 1})
    return unpack(doc["vector"]) if doc and doc.get("vector") is not None else None


async def drop(db, article_id: str) -> None:
    try:
        await db[ndb.ARTICLE_VECTORS].delete_one({"_id": article_id})
    except Exception:
        logger.exception(f"[NEWSROOM] Could not remove the pending vector of article {article_id}")


async def refresh(db, articles, article_id: str) -> str:
    """Makes, keeps or removes an article's vector to match its status and text. Returns what happened:
    "saved", "unchanged", "removed", "skipped" (switched off / no text / not found) or "failed"."""
    try:
        article = await articles.find_one({"_id": article_id}, {"status": 1, "final_output.title": 1,
                                                                 "final_output.summary": 1})
        if article is None or article.get("status") != PENDING:
            await drop(db, article_id)
            return "removed"
        text = vector_text(article.get("final_output"))
        if not settings.NEWSROOM_PENDING_VECTORS_ENABLED or not text:
            return "skipped"
        text_hash = _text_hash(text)
        if await db[ndb.ARTICLE_VECTORS].find_one({"_id": article_id, "text_hash": text_hash}, {"_id": 1}):
            return "unchanged"  # same title and summary: never paid twice
        with tracing.job_span("article_match_vector", tags=["embeddings", "newsroom"],
                              metadata={"article_id": article_id}, input_value=text) as span:
            vector = await make_vector(text)
            tracing.set_output(span, {"outcome": "saved", "size": len(vector)})
        await db[ndb.ARTICLE_VECTORS].replace_one(
            {"_id": article_id},
            {"_id": article_id, "vector": pack(vector), "model": settings.MONGO_EMBEDDING_MODEL,
             "text_hash": text_hash, "made_at": datetime.now(timezone.utc)},
            upsert=True)
        return "saved"
    except Exception:
        logger.exception(f"[NEWSROOM] Pending vector failed for article {article_id}")
        return "failed"
