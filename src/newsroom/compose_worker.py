"""
Compose jobs (worker side): the only Newsroom code that calls an AI model.

Researcher -> Writer -> Editor with NEWSROOM_COMPOSE_MODEL, sources passed as
data only. Articles then go through the normal pipeline from the summary step
(compose_graph) and are stored through the result webhook as a pending article
by "Propt Newsroom". Signals are kept as a draft on the run for the reviewer.
Token usage and cost of every call (including the pipeline steps) are recorded
on the run and count towards the daily budget.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import httpx
from langchain_community.callbacks.manager import get_openai_callback
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from src.configs.settings import settings
from src.models.ArticleModel import ArticleModel
from src.models.MainWorkflowState import MainWorkflowState
from src.newsroom import db as ndb
from src.newsroom import links as lnk
from src.newsroom.compose import AUTHOR, COMPOSED_SOURCE_ID, composed_url
from src.newsroom.prompts import PROMPTS_BY_NAME

logger = logging.getLogger("newsroom")

SOURCE_TEXT_CHARS = 6000
WEBHOOK_ATTEMPTS = 3


# --- What the models must return -------------------------------------------------

class ResearchNotes(BaseModel):
    key_facts: List[str] = Field(default_factory=list, description="Key facts, each ending with its source number, e.g. (source 2)")
    numbers: List[str] = Field(default_factory=list, description="Every number, date and amount exactly as written, with its source number")
    agreements_and_conflicts: str = Field("", description="Where the sources agree, add to each other or conflict")
    angle: str = Field("", description="One angle connecting the sources for real-estate investors")


class ArticleDraft(BaseModel):
    title: str
    content: str = Field(..., description="Plain paragraphs separated by blank lines")
    changes: List[str] = Field(default_factory=list, description="Editor only: what was corrected")


class SignalDraft(BaseModel):
    summary_en: str
    summary_ar: str
    signal_category: str
    regions: List[str] = Field(default_factory=list)
    areas_mentioned: List[str] = Field(default_factory=list)
    entities_involved: List[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0, le=100)
    changes: List[str] = Field(default_factory=list, description="Editor only: what was corrected")


class ExtractDecision(BaseModel):
    is_signal: bool
    reason: str = Field("", description="One sentence; required when it is not a signal")
    draft: Optional[SignalDraft] = None


# --- Helpers (replaceable in tests) ----------------------------------------------

def get_compose_model() -> ChatOpenAI:
    return ChatOpenAI(model=settings.NEWSROOM_COMPOSE_MODEL, temperature=0.3, openai_api_key=settings.OPENAI_API_KEY)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(text: str) -> str:
    # Source text can never close its own <source> tag.
    return (text or "").replace("</source>", "</ source>").replace("<source", "< source")


async def load_prompts(names: List[str]) -> dict:
    """Active prompts from the registry; the built-in default when one is missing."""
    from src.graph.nodes.load_agent_configuration import get_async_config_database

    found = {}
    try:
        async for doc in get_async_config_database()["prompts"].find({"name": {"$in": names}, "status": "active"}):
            found[doc["name"]] = doc["content"]
    except Exception as e:
        logger.warning(f"[NEWSROOM] Prompt registry unavailable, using defaults: {e}")
    prompts = {}
    for name in names:
        if name not in found:
            logger.warning(f"[NEWSROOM] Prompt {name} not in the registry; using the built-in default")
        prompts[name] = found.get(name) or PROMPTS_BY_NAME[name]["content"]
    return prompts


async def load_source_texts(sources: ndb.ItemSources, run: dict) -> List[dict]:
    """Full text of each source, read by NewsAgent itself (never sent by the browser)."""
    from bson import ObjectId

    texts = []
    for s in run["sources"]:
        if s["source_type"] == "article":
            doc = await sources.articles.find_one({"_id": s["source_id"]})
            final = (doc or {}).get("final_output") or {}
            text = final.get("content") or final.get("summary") or ""
            title, url = final.get("title") or s["title"], (doc or {}).get("url")
        else:
            doc = await sources.signals.find_one({"_id": ObjectId(s["source_id"])}) if ObjectId.is_valid(s["source_id"]) else None
            doc = doc or {}
            evidence = "\n".join(f"- {e.get('content_snippet', '')}" for e in doc.get("evidence") or [] if isinstance(e, dict))
            text = (doc.get("summary_en") or doc.get("summary") or "") + ("\nEvidence:\n" + evidence if evidence else "")
            title, url = s["title"], s.get("url")
        if not doc:
            raise ValueError(f"Source {s['source_type']} {s['source_id']} no longer exists")
        texts.append({"type": s["source_type"], "title": title, "url": url, "text": text[:SOURCE_TEXT_CHARS]})
    return texts


def format_sources(texts: List[dict]) -> str:
    return "\n\n".join(
        f'<source n="{n}" type="{t["type"]}" title="{_clean(t["title"] or "")}" url="{t["url"] or ""}">\n{_clean(t["text"])}\n</source>'
        for n, t in enumerate(texts, start=1)
    )


async def entity_context(db, entity_ids: List[str]) -> str:
    if not entity_ids:
        return "(none confirmed)"
    lines = []
    async for e in db[ndb.ENTITIES].find({"_id": {"$in": entity_ids}}, {"canonical_name": 1, "kind": 1, "type": 1}):
        lines.append(f"- {e['canonical_name']} ({e['type']})")
    return "\n".join(sorted(lines))


async def ask(model, prompt: str, variables: dict, schema):
    content = PromptTemplate.from_template(prompt).format(**variables)
    return await model.with_structured_output(schema).ainvoke([("system", content), ("user", "Return the result.")])


async def post_to_webhook(payload: dict) -> None:
    """The existing result webhook stores the article; retried with backoff because failures are not retried elsewhere."""
    if not settings.WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL is not configured")
    headers = {"Content-Type": "application/json", "User-Agent": "NewsAgent/1.0"}
    if settings.WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = settings.WEBHOOK_SECRET
    last = None
    for attempt in range(WEBHOOK_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(settings.WEBHOOK_URL, json=payload, headers=headers)
            if response.status_code < 300:
                return
            last = f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as e:
            last = str(e)
        await asyncio.sleep(3 ** attempt)
    raise RuntimeError(f"Storing the article failed: {last}")


async def run_article_pipeline(run_id: str, draft: ArticleDraft, texts: List[dict]) -> Tuple[ArticleModel, Optional[dict]]:
    """The normal article steps from the summary onwards (compose_graph)."""
    from src.newsroom.compose_graph import get_compose_graph

    state = MainWorkflowState(
        source_url=composed_url(run_id),
        cleaned_article_text=draft.content,
        news_article=ArticleModel(title=draft.title, content=draft.content, author=AUTHOR,
                                  published_date=_now().isoformat()),
        # The chosen sources replace the web search for "other sources" (used for "why this matters").
        other_sources=[{"title": t["title"], "url": t["url"], "source": "Newsroom source", "content": t["text"]}
                       for t in texts if t["url"]],
    )
    final = await get_compose_graph().ainvoke(state)
    if final.get("error_message"):
        raise RuntimeError(final["error_message"])
    article: ArticleModel = final["news_article"]
    article.author = AUTHOR
    if article.seo and isinstance(article.seo.json_ld_schema, dict):
        # The internal newsroom:// address must never appear in published page data.
        page = article.seo.json_ld_schema.get("mainEntityOfPage")
        if isinstance(page, dict):
            page.pop("@id", None)
    return article, final.get("newsroom_names")


# --- The job ----------------------------------------------------------------------

async def run_compose_job(run_id: str, db=None, sources: Optional[ndb.ItemSources] = None) -> None:
    db = db if db is not None else ndb.get_newsroom_db()
    sources = sources or ndb.get_item_sources()
    run = await db[ndb.COMPOSE_RUNS].find_one({"_id": run_id})
    if run is None or run["status"] != "queued":
        return
    await db[ndb.COMPOSE_RUNS].update_one({"_id": run_id}, {"$set": {"status": "running", "started_at": _now()}})

    update = {}
    with get_openai_callback() as usage:
        try:
            result, status = await asyncio.wait_for(
                _compose(db, sources, run, update), timeout=settings.NEWSROOM_COMPOSE_TIMEOUT_MINUTES * 60)
            update.update({"status": status, "result": result})
        except Exception as e:
            logger.exception(f"[NEWSROOM] Compose run {run_id} failed")
            update.update({"status": "failed", "error": (str(e) or type(e).__name__)[:500]})
        finally:
            update.update({
                "finished_at": _now(),
                "tokens": {"prompt": usage.prompt_tokens, "completion": usage.completion_tokens, "total": usage.total_tokens},
                "cost_usd": round(float(usage.total_cost), 6),
            })
            await db[ndb.COMPOSE_RUNS].update_one({"_id": run_id}, {"$set": update})


async def _compose(db, sources, run, update: dict):
    kind, output_type = run["kind"], run["output_type"]
    if kind == "extract_signal":
        names = ["newsroom_researcher", "newsroom_extract_signal", "newsroom_editor"]
    elif output_type == "signal":
        names = ["newsroom_researcher", "newsroom_writer_signal", "newsroom_editor"]
    else:
        names = ["newsroom_researcher", "newsroom_writer_article", "newsroom_editor"]
    prompts = await load_prompts(names)
    update["prompts"] = {n: hashlib.sha256(p.encode()).hexdigest()[:12] for n, p in prompts.items()}

    texts = await load_source_texts(sources, run)
    source_block = format_sources(texts)
    model = get_compose_model()

    research = await ask(model, prompts["newsroom_researcher"],
                         {"sources": source_block, "entities": await entity_context(db, run["entity_ids"])}, ResearchNotes)
    notes = research.model_dump_json(indent=1)

    if kind == "extract_signal":
        decision = await ask(model, prompts["newsroom_extract_signal"], {"research": notes, "sources": source_block}, ExtractDecision)
        if not decision.is_signal or decision.draft is None:
            return {"reason": decision.reason or "The article does not describe a specific market movement."}, "not_a_signal"
        draft = decision.draft
    elif output_type == "signal":
        draft = await ask(model, prompts["newsroom_writer_signal"], {"research": notes, "sources": source_block}, SignalDraft)
    else:
        draft = await ask(model, prompts["newsroom_writer_article"], {"research": notes, "sources": source_block}, ArticleDraft)

    edited = await ask(model, prompts["newsroom_editor"],
                       {"draft": draft.model_dump_json(indent=1), "sources": source_block}, type(draft))

    if output_type == "signal":
        signal = edited.model_dump()
        signal["entities_involved"] = signal.get("entities_involved") or []
        return {"draft": signal, "author": AUTHOR,
                "article_id": run["sources"][0]["source_id"] if kind == "extract_signal" else None}, "succeeded"

    article, newsroom_names = await run_article_pipeline(run["_id"], ArticleDraft(**edited.model_dump()), texts)
    url = composed_url(run["_id"])
    payload = {
        "source_url": url,
        "status": "success",
        "data": article.model_dump(mode="json"),
        "newsroom": {"generated_from": run["_id"], "author": AUTHOR, "source_id": COMPOSED_SOURCE_ID},
    }
    if newsroom_names:
        payload["newsroom_names"] = newsroom_names
    await post_to_webhook(payload)

    stored = await sources.articles.find_one({"url": url}, {"_id": 1})
    if stored is None:
        raise RuntimeError("The article was sent but cannot be found")
    # The sources' confirmed companies and places become links of the new article.
    for entity_id in run["entity_ids"]:
        try:
            await lnk.link(db, sources, "article", stored["_id"], entity_id, run["requested_by"], link_source="compose")
        except Exception as e:
            logger.warning(f"[NEWSROOM] Could not link {entity_id} to composed article: {e}")
    return {"article_id": stored["_id"], "title": article.title, "editor_changes": edited.changes, "author": AUTHOR}, "succeeded"
