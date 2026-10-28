"""
Compose jobs (worker side): the only Newsroom code that calls an AI model.

Researcher -> Writer -> Editor with NEWSROOM_COMPOSE_MODEL, sources passed as
data only. The Writer uses NEWSROOM_COMPOSE_TEMPERATURE; the Researcher, the
Editor and the extract-signal decision use NEWSROOM_COMPOSE_CHECK_TEMPERATURE. Articles then go through the normal pipeline from the summary step
(compose_graph) and are stored through the result webhook as a pending article
by "Propt Newsroom". Signals are kept as a draft on the run for the reviewer.
Token usage and cost of every call (including the pipeline steps) are recorded
on the run and count towards the daily budget. Every call is also traced in Opik
(one thread per run, tagged newsroom/compose) when OPIK_API_KEY is set; tracing
problems are logged and never fail a job.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Literal, Optional, Tuple

import httpx
from langchain_community.callbacks.manager import get_openai_callback
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from src.configs.settings import settings
from src.models.ArticleModel import ArticleModel
from src.models.MainWorkflowState import MainWorkflowState
from src.newsroom import compose as cmp
from src.newsroom import db as ndb
from src.newsroom import links as lnk
from src.newsroom.compose import AUTHOR, COMPOSED_SOURCE_ID, composed_url
from src.newsroom.desks import byline
from src.newsroom.prompts import PROMPTS_BY_NAME
from src.utils import tracing
from src.newsroom.public_links import shown_url
from src.newsroom.signal_options import GCC_REGIONS, REAL_ESTATE_CATEGORIES, SIGNAL_ANGLES, SIGNAL_CATEGORIES

logger = logging.getLogger("newsroom")

SOURCE_TEXT_CHARS = 6000
WEBHOOK_ATTEMPTS = 3


# --- What the models must return -------------------------------------------------

class ResearchNotes(BaseModel):
    key_facts: List[str] = Field(default_factory=list, description="Key facts, each ending with its source number, e.g. (source 2)")
    numbers: List[str] = Field(default_factory=list, description="Every number, date and amount exactly as written, with its source number")
    agreements_and_conflicts: str = Field("", description="Where the sources agree, add to each other or conflict")
    angle: str = Field("", description="The core story and why a reader cares right now, as one angle connecting the sources")


class ArticleDraft(BaseModel):
    title: str
    content: str = Field(..., description="Plain paragraphs separated by blank lines")
    changes: List[str] = Field(default_factory=list, description="Editor only: what was corrected")


# Grok Scout's lists (signal_options.py): the model can only answer with these values, so a saved
# signal gets its category and countries (ids, Arabic names) like a Grok signal.
SignalCategory = Literal[tuple(SIGNAL_CATEGORIES)]
SignalAngle = Literal[tuple(SIGNAL_ANGLES)]
GccRegion = Literal[tuple(GCC_REGIONS)]
RealEstateCategory = Literal[tuple(REAL_ESTATE_CATEGORIES)]


class SignalDraft(BaseModel):
    # Same length as Grok Scout's signals (social_scrapper grok_scout.py)
    summary_en: str = Field(..., description="Concise one-sentence summary in English (10-15 words). Must include entity and action.")
    summary_ar: str = Field(..., description="Concise one-sentence summary in Arabic (10-15 words).")
    signal_category: SignalCategory
    signal_angle: SignalAngle
    regions: List[GccRegion] = Field(default_factory=list)
    categories: List[RealEstateCategory] = Field(default_factory=list, max_length=3)
    areas_mentioned: List[str] = Field(default_factory=list)
    entities_involved: List[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0, le=100)
    changes: List[str] = Field(default_factory=list, description="Editor only: what was corrected")


class ExtractDecision(BaseModel):
    is_signal: bool
    reason: str = Field("", description="One sentence; required when it is not a signal")
    drafts: List[SignalDraft] = Field(default_factory=list, max_length=cmp.MAX_SIGNAL_DRAFTS,
                                      description="1-3 drafts, at most one per signal_angle, only angles the article states")


def one_per_angle(drafts: List[SignalDraft]) -> List[SignalDraft]:
    """At most one draft per angle and at most MAX_SIGNAL_DRAFTS, in the model's order."""
    kept, angles = [], set()
    for d in drafts:
        if d.signal_angle not in angles and len(kept) < cmp.MAX_SIGNAL_DRAFTS:
            angles.add(d.signal_angle)
            kept.append(d)
    return kept


# --- Helpers (replaceable in tests) ----------------------------------------------

def get_compose_model(temperature: float) -> ChatOpenAI:
    return ChatOpenAI(model=settings.NEWSROOM_COMPOSE_MODEL, temperature=temperature, openai_api_key=settings.OPENAI_API_KEY)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _escaped(rules: str) -> str:
    """Rules added to a prompt template are never read as template inputs."""
    return rules.replace("{", "{{").replace("}", "}}")


def _clean(text: str) -> str:
    # Source text can never close its own <source> tag.
    return (text or "").replace("</source>", "</ source>").replace("<source", "< source")


class PromptsUnavailable(RuntimeError):
    """A compose prompt cannot be read from the registry: the job fails before any AI call."""


async def load_prompts(names: List[str]) -> dict:
    """Active prompts from the registry (the database) only. If the registry cannot be read, or a prompt is missing
    or lacks one of its inputs (e.g. {sources}), the job fails: no built-in text is used instead (user decision
    2026-10-22), so what runs is always what init_db.py wrote."""
    from src.graph.nodes.load_agent_configuration import get_async_config_database

    found = {}
    try:
        async for doc in get_async_config_database()["prompts"].find({"name": {"$in": names}, "status": "active"}):
            found[doc["name"]] = doc["content"]
    except Exception as e:
        message = f"Critical Configuration Error: the prompt registry could not be read: {e}"
        logger.error(f"[NEWSROOM] {message}")
        raise PromptsUnavailable(message) from e
    problems = []
    for name in names:
        content = found.get(name)
        if not content:
            problems.append(f"{name} is missing")
            continue
        # A prompt without e.g. {sources} would make the writer work from nothing.
        needed = set(PROMPTS_BY_NAME[name]["input_variables"])
        missing = needed - set(PromptTemplate.from_template(content).input_variables)
        if missing:
            problems.append(f"{name} lacks {', '.join('{' + m + '}' for m in sorted(missing))}")
    if problems:
        # Same wording as the article pipeline's prompt loading, so one log search finds both
        message = f"Critical Configuration Error: prompts not usable: {'; '.join(problems)}. Run init_db.py --mode upsert."
        logger.error(f"[NEWSROOM] {message}")
        raise PromptsUnavailable(message)
    return {name: found[name] for name in names}


async def load_source_texts(sources: ndb.ItemSources, run: dict) -> List[dict]:
    """Full text of each source, read by NewsAgent itself (never sent by the browser)."""
    from bson import ObjectId

    texts = []
    for s in run["sources"]:
        if s["source_type"] == "article":
            doc = await sources.articles.find_one({"_id": s["source_id"]})
            final = (doc or {}).get("final_output") or {}
            text = final.get("content") or final.get("summary") or ""
            # A composed article used as a source is shown by its public link, never newsroom://
            title, url = final.get("title") or s["title"], shown_url(doc) if doc else None
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


def get_compose_tracer(run: dict):
    """An Opik tracer for one compose run, or None (Opik not configured or unavailable)."""
    if not settings.OPIK_API_KEY:
        return None
    try:
        return settings.get_opik_client(
            tags=list(dict.fromkeys(t for t in ("newsroom", "compose", run.get("kind"), run.get("output_type")) if t)),
            metadata={"compose_run_id": run["_id"], "kind": run.get("kind"), "output_type": run.get("output_type"),
                      "model": settings.NEWSROOM_COMPOSE_MODEL, "requested_by": run.get("requested_by"),
                      "sources": run.get("source_keys") or []},
            thread_id=run["_id"],
        )
    except Exception as e:
        logger.warning(f"[NEWSROOM] Opik tracing unavailable for compose run {run.get('_id')}: {e}")
        return None


def _config(tracer) -> dict:
    return {"callbacks": [tracer]} if tracer is not None else {}


async def ask(model, prompt: str, variables: dict, schema, tracer=None):
    content = PromptTemplate.from_template(prompt).format(**variables)
    return await model.with_structured_output(schema).ainvoke(
        [("system", content), ("user", "Return the result.")], config=_config(tracer))


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


async def run_article_pipeline(run_id: str, draft: ArticleDraft, texts: List[dict],
                               tracer=None, author: str = AUTHOR,
                               wtm_rules: Optional[str] = None) -> Tuple[ArticleModel, Optional[dict]]:
    """The normal article steps from the summary onwards (compose_graph). wtm_rules: added to this
    article's "why this matters" prompt (the newsroom rules, and the chosen desk's rules)."""
    from src.newsroom.compose_graph import get_compose_graph

    state = MainWorkflowState(
        source_url=composed_url(run_id),
        cleaned_article_text=draft.content,
        news_article=ArticleModel(title=draft.title, content=draft.content, author=author,
                                  published_date=_now().isoformat()),
        # The chosen sources replace the web search for "other sources" (used for "why this matters").
        other_sources=[{"title": t["title"], "url": t["url"], "source": "Newsroom source", "content": t["text"]}
                       for t in texts if t["url"]],
    )
    final = await get_compose_graph(wtm_rules).ainvoke(state, config=_config(tracer))
    if final.get("error_message"):
        raise RuntimeError(final["error_message"])
    article: ArticleModel = final["news_article"]
    article.author = author
    if article.seo and isinstance(article.seo.json_ld_schema, dict):
        # The internal newsroom:// address must never appear in published page data.
        page = article.seo.json_ld_schema.get("mainEntityOfPage")
        if isinstance(page, dict):
            page.pop("@id", None)
    return article, final.get("newsroom_names")


# --- The job ----------------------------------------------------------------------

def run_cost(usage) -> float:
    """What the calls cost. A model missing from the price list reports $0 although tokens were used,
    which would switch the daily cap off: then the fallback price (gpt-4o by default) is used."""
    cost = float(usage.total_cost or 0)
    if cost == 0 and usage.total_tokens:
        cost = (usage.prompt_tokens * settings.NEWSROOM_FALLBACK_INPUT_USD_PER_1M
                + usage.completion_tokens * settings.NEWSROOM_FALLBACK_OUTPUT_USD_PER_1M) / 1_000_000
        logger.warning(f"[NEWSROOM] No price known for model {settings.NEWSROOM_COMPOSE_MODEL}; "
                       f"counting {usage.total_tokens} tokens at the fallback price (${cost:.6f})")
    return round(cost, 6)


def _trace_run_outcome(span, update: dict, usage) -> None:
    """How the run ended, at the top of its trace, with this start's cost as the daily budget counts it
    (to compare with the cost Phoenix works out from the tokens)."""
    status = update.get("status")
    span.set_attribute("newsroom.cost_usd", run_cost(usage))
    span.set_attribute("newsroom.tokens_total", usage.total_tokens or 0)
    tracing.set_output(span, {"status": status, "error": update.get("error"), "result": update.get("result")})
    if status == "failed":
        tracing.mark_failed(span, update.get("error") or "failed")


REQUEUE = "requeue"
INTERRUPTED = "Stopped {n} times because the worker was shutting down; try again."


async def run_compose_job(run_id: str, db=None, sources: Optional[ndb.ItemSources] = None) -> Optional[str]:
    """
    Runs one queued compose job. Returns REQUEUE when a worker shutdown cut it off and it
    should go back in the queue (the caller pushes it back); otherwise None.
    """
    db = db if db is not None else ndb.get_newsroom_db()
    sources = sources or ndb.get_item_sources()
    # Claim it in one step, so the same job delivered twice is never run (and paid for) twice.
    started = _now()
    run = await db[ndb.COMPOSE_RUNS].find_one_and_update(
        {"_id": run_id, "status": "queued"},
        {"$set": {"status": "running", "started_at": started, "deadline_at": cmp.run_deadline(started)},
         "$inc": {"attempts": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if run is None:
        return None  # unknown, or no longer queued (finished, failed or given up)

    # One trace per run in Phoenix: every step (Researcher, Writer, Editor, the article or signal steps) nests
    # under it, grouped by the run id; the top shows the run's totals and how it ended.
    with tracing.job_span(
        f"compose_{run['kind']}", session_id=run_id, input_value=", ".join(run.get("source_keys") or []),
        tags=list(dict.fromkeys(t for t in ("newsroom", "compose", run.get("kind"), run.get("output_type")) if t)),
        metadata={"compose_run_id": run_id, "kind": run.get("kind"), "output_type": run.get("output_type"),
                  "model": settings.NEWSROOM_COMPOSE_MODEL, "requested_by": run.get("requested_by"),
                  "sources": run.get("source_keys") or [], "attempt": run.get("attempts", 1)},
    ) as run_trace:
        update = {}
        tracer = get_compose_tracer(run)
        update["opik_traced"] = tracer is not None
        interrupted = False
        with get_openai_callback() as usage:
            async def checkpoint() -> None:
                """After each paid step: today's spend sees this job's cost so far (kept even if the worker dies)."""
                await db[ndb.COMPOSE_RUNS].update_one(
                    {"_id": run_id, "status": "running"}, {"$set": {"running_cost_usd": run_cost(usage)}})

            try:
                result, status = await asyncio.wait_for(
                    _compose(db, sources, run, update, tracer, checkpoint), timeout=settings.NEWSROOM_COMPOSE_TIMEOUT_MINUTES * 60)
                update.update({"status": status, "result": result, "finished_at": _now()})
            except asyncio.CancelledError:
                # The worker is shutting down: wait in the queue again unless it has had its starts.
                interrupted = run.get("attempts", 1) < settings.NEWSROOM_COMPOSE_MAX_ATTEMPTS
                if interrupted:
                    update.update({"status": "queued", "started_at": None, "deadline_at": cmp.wait_deadline(_now()),
                                   "error": None})
                else:
                    update.update({"status": "failed", "finished_at": _now(),
                                   "error": INTERRUPTED.format(n=run.get("attempts", 1))})
            except Exception as e:
                logger.exception(f"[NEWSROOM] Compose run {run_id} failed")
                update.update({"status": "failed", "error": (str(e) or type(e).__name__)[:500], "finished_at": _now()})
            finally:
                # Spend adds up over every start; written only while this worker still owns the run,
                # so a run the clean-up already marked failed is not turned back into a success.
                spent = {
                    "tokens.prompt": usage.prompt_tokens,
                    "tokens.completion": usage.completion_tokens,
                    "tokens.total": usage.total_tokens,
                    "cost_usd": run_cost(usage),
                }
                # The running figure becomes part of cost_usd now.
                res = await db[ndb.COMPOSE_RUNS].update_one(
                    {"_id": run_id, "status": "running"},
                    {"$set": update, "$inc": spent, "$unset": {"running_cost_usd": ""}},
                )
                if res.matched_count == 0:
                    # Already given up (timed out): still count what was spent.
                    await db[ndb.COMPOSE_RUNS].update_one(
                        {"_id": run_id}, {"$inc": spent, "$unset": {"running_cost_usd": ""}})
                    logger.warning(f"[NEWSROOM] Compose run {run_id} ended after it was given up; its result was not kept")
                    interrupted = False
        _trace_run_outcome(run_trace, update, usage)
    if tracer is not None:
        try:
            tracer.flush()
        except Exception as e:
            logger.warning(f"[NEWSROOM] Opik flush failed for compose run {run_id}: {e}")
    return REQUEUE if interrupted else None


async def _compose(db, sources, run, update: dict, tracer=None, checkpoint=None):
    kind, output_type = run["kind"], run["output_type"]
    if output_type == "article" and not settings.WEBHOOK_URL:
        # Checked before any paid call: the article could not be stored at the end.
        raise RuntimeError("WEBHOOK_URL is not configured on this worker; nothing was spent")

    async def paid(step):
        result = await step
        if checkpoint is not None:
            await checkpoint()
        return result

    if kind == "extract_signal":
        names = ["newsroom_researcher", "newsroom_extract_signal", "newsroom_editor"]
    elif output_type == "signal":
        names = ["newsroom_researcher", "newsroom_writer_signal", "newsroom_editor"]
    else:
        names = ["newsroom_researcher", "newsroom_writer_article", "newsroom_editor", "newsroom_rules",
                 "newsroom_editor_rules"]
    type_prompt = f"newsroom_type_{run['compose_type']}" if run.get("compose_type") else None
    desk_prompt = f"newsroom_desk_{run['desk']}" if run.get("desk") else None
    extra = [p for p in (type_prompt, desk_prompt) if p]
    prompts = await load_prompts(names + extra)
    update["prompts"] = {n: hashlib.sha256(p.encode()).hexdigest()[:12] for n, p in prompts.items()}
    for name in extra:
        # The chosen type's and desk's rules go to the researcher (which facts and angle) and the writer.
        # Escaped so the rules are never read as template inputs.
        rules = "\n\n" + _escaped(prompts[name])
        for target in ("newsroom_researcher", "newsroom_writer_article"):
            prompts[target] += rules
    if output_type == "article":
        # Every composed article: the researcher and the editor follow the newsroom rules too (the writer's
        # prompt has them). The editor never gets a desk's voice.
        prompts["newsroom_researcher"] += "\n\n" + _escaped(prompts["newsroom_rules"])
        prompts["newsroom_editor"] += "\n\n" + _escaped(prompts["newsroom_editor_rules"])

    texts = await load_source_texts(sources, run)
    source_block = format_sources(texts)
    checker = get_compose_model(settings.NEWSROOM_COMPOSE_CHECK_TEMPERATURE)
    writer = get_compose_model(settings.NEWSROOM_COMPOSE_TEMPERATURE)

    research = await paid(ask(checker, prompts["newsroom_researcher"],
                         {"sources": source_block, "entities": await entity_context(db, run["entity_ids"])}, ResearchNotes, tracer))
    notes = research.model_dump_json(indent=1)

    async def edit(draft):
        return await paid(ask(checker, prompts["newsroom_editor"],
                              {"draft": draft.model_dump_json(indent=1), "sources": source_block}, type(draft), tracer))

    if output_type == "signal":
        if kind == "extract_signal":
            decision = await paid(ask(checker, prompts["newsroom_extract_signal"], {"research": notes, "sources": source_block}, ExtractDecision, tracer))
            drafts = one_per_angle(decision.drafts) if decision.is_signal else []
            if not drafts:
                return {"reason": decision.reason or "The article does not describe a specific market movement."}, "not_a_signal"
        else:
            drafts = [await paid(ask(writer, prompts["newsroom_writer_signal"], {"research": notes, "sources": source_block}, SignalDraft, tracer))]
        # Each draft is checked on its own; the editor may change an angle, so one per angle is kept again.
        edited = one_per_angle([await edit(d) for d in drafts])
        signals = [{**d.model_dump(), "entities_involved": d.entities_involved or []} for d in edited]
        return {"drafts": signals, "author": AUTHOR,
                "article_id": run["sources"][0]["source_id"] if kind == "extract_signal" else None}, "succeeded"

    draft = await paid(ask(writer, prompts["newsroom_writer_article"], {"research": notes, "sources": source_block}, ArticleDraft, tracer))
    edited = await edit(draft)

    author = byline(run.get("desk"))
    article, newsroom_names = await paid(run_article_pipeline(
        run["_id"], ArticleDraft(**edited.model_dump()), texts, tracer, author=author,
        wtm_rules=prompts["newsroom_rules"] + (f"\n\n{prompts[desk_prompt]}\nWrite \"why this matters\" for these readers."
                                    if desk_prompt else "")))
    url = composed_url(run["_id"])
    payload = {
        "source_url": url,
        "status": "success",
        "data": article.model_dump(mode="json"),
        "newsroom": {"generated_from": run["_id"], "author": author, "source_id": COMPOSED_SOURCE_ID},
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
    return {"article_id": stored["_id"], "title": article.title, "editor_changes": edited.changes, "author": author}, "succeeded"
