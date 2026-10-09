"""
Backfill (step 8): links on items that were approved before the Newsroom existed.

Rules (user decisions, 2026-10-10; trial in handoff 27.11-27.13):
- Approved articles and approved signals only.
- Names: signals carry them (entities_involved; areas_mentioned + regions).
  Articles: names already extracted (existing suggestions), else a names cache
  (the trial's model_names.jsonl), else the real name step (extract_country with
  the init_db prompts), capped by the caller.
- Every name goes through links.suggest, exactly like live items.
- Auto-link (option A) only an open suggestion of a switched-on entity whose name
  (or matched alias) is written exactly that way in the item. Everything else
  stays a suggestion; unknown names go to the new-names queue (D4).
- Never re-links what a reviewer removed or dismissed (suggest/link already
  guarantee this). Links carry link_source "backfill" so undo() can remove
  them all at once.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bson import ObjectId

from src.newsroom import backfill_trial as bt
from src.newsroom import db as ndb
from src.newsroom import entities as ent
from src.newsroom import links as lnk
from src.newsroom import resolve as rs
from src.newsroom.locations import refresh_item_locations
from src.newsroom.normalize import normalize_alias

ACTOR = "backfill"
UNDO_ACTOR = "backfill-undo"
LINK_SOURCE = "backfill"

# Actions in the plan / result
AUTO_LINK = "auto_link"
SUGGEST_NOT_WRITTEN = "suggest_not_written_exactly"
SUGGEST_SWITCHED_OFF = "suggest_switched_off"
NEW_NAME = "new_name"
ALREADY_DONE = "already_linked_removed_or_decided"


def _strings(values) -> List[str]:
    return [v.strip() for v in (values or []) if isinstance(v, str) and v.strip()]


def signal_names(doc: dict) -> Tuple[List[str], List[str]]:
    """The names social_scrapper sends for a signal (newsroom_client.signal_names)."""
    return _strings(doc.get("entities_involved")), _strings(doc.get("areas_mentioned")) + _strings(doc.get("regions"))


def item_text(source_type: str, doc: dict) -> str:
    if source_type == "article":
        return bt.article_text(doc)
    return "\n".join(str(doc.get(k) or "") for k in ("summary_en", "summary"))


def written_exactly(name: str, alias: Optional[str], text: str) -> bool:
    """The safeguard: the found name, or the alias it matched, appears in the text with the same capitals."""
    link = bt.NameResult(field="", name=name, outcome=rs.ACTIVE, matched_alias=alias)
    return bt.evidence(link, text)[0] == bt.EXACT_CASE


# --- Names for articles -------------------------------------------------------------

def init_db_prompts():
    """The prompts init_db.py installs (what the pipeline uses after `init_db.py --mode upsert`)."""
    from src.graph.nodes.load_agent_configuration import REQUIRED_PROMPTS
    from src.models.AgentPromptsModel import AgentPromptsModel
    from src.scripts.init_db import INITIAL_DATA
    return AgentPromptsModel(**{p["name"]: p["content"] for p in INITIAL_DATA if p["name"] in REQUIRED_PROMPTS})


async def ai_article_names(article: dict, prompts) -> Tuple[Optional[dict], Optional[str]]:
    """The real name step on a stored article: (names, error). Needs NEWSROOM_ENABLED (the step only
    returns names when it is on). Input: the stored title, summary and content (raw text is not kept)."""
    from src.graph.nodes import extract_country as node
    from src.models.ArticleModel import ArticleModel
    from src.models.MainWorkflowState import MainWorkflowState

    final = article.get("final_output") or {}
    content = str(final.get("content") or "")
    state = MainWorkflowState(
        source_url=article.get("url") or str(article.get("_id")),
        cleaned_article_text=content,
        news_article=ArticleModel(title=str(final.get("title") or ""), content=content, summary=str(final.get("summary") or "")),
        active_prompts=prompts,
    )
    result = await node.extract_country(state)
    return result.newsroom_names, result.error_message


def read_names_cache(path: Optional[Path]) -> Dict[str, dict]:
    """article id -> names, from a JSONL file of {"article_id", "names", "error"} (failed rows are skipped)."""
    cache = {}
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if not record.get("error") and record.get("names") is not None:
                    cache[record["article_id"]] = record["names"]
    return cache


async def existing_names(db, source_type: str, source_id: str) -> Optional[Tuple[List[str], List[str]]]:
    """Names already extracted for the item (live tagging stored them as suggestions), or None."""
    docs = await db[ndb.ENTITY_SUGGESTIONS].find(
        {"source_type": source_type, "source_id": source_id}, {"field": 1, "name": 1}).to_list(length=None)
    if not docs:
        return None
    return ([d["name"] for d in docs if d["field"] == "organisation"],
            [d["name"] for d in docs if d["field"] == "location"])


# --- Items ----------------------------------------------------------------------------

async def approved_items(sources: ndb.ItemSources, source_type: str, limit: Optional[int] = None) -> List[dict]:
    collection = sources.articles if source_type == "article" else sources.signals
    projection = ({"final_output.title": 1, "final_output.summary": 1, "final_output.content": 1, "url": 1, "status": 1}
                  if source_type == "article" else None)
    cursor = collection.find({"status": "approved"}, projection).sort("_id", 1)
    if limit:
        cursor = cursor.limit(limit)
    return await cursor.to_list(length=None)


def item_id(doc: dict) -> str:
    return str(doc["_id"]) if isinstance(doc["_id"], ObjectId) else doc["_id"]


def item_title(source_type: str, doc: dict) -> str:
    if source_type == "article":
        return str((doc.get("final_output") or {}).get("title") or "")
    return str(doc.get("summary_en") or doc.get("summary") or "")[:120]


# --- Plan (dry run) and apply ---------------------------------------------------------

async def load_index(db) -> bt.ListIndex:
    """The current lists from the database (reviewers' edits included)."""
    entities = await db[ndb.ENTITIES].find({}).to_list(length=None)
    aliases = await db[ndb.ENTITY_ALIASES].find({}).to_list(length=None)
    ignored = [d["string_key"] async for d in db[ndb.ENTITY_IGNORE].find({}, {"string_key": 1})]
    return bt.ListIndex(entities, aliases, ignored)


async def plan_item(db, index: bt.ListIndex, source_type: str, doc: dict,
                    organisations: List[str], locations: List[str]) -> List[dict]:
    """What apply_item would do, without writing. One row per distinct name."""
    source_id = item_id(doc)
    text = item_text(source_type, doc)
    mentions = {m["entity_id"] async for m in db[ndb.ENTITY_MENTIONS].find(
        {"source_type": source_type, "source_id": source_id}, {"entity_id": 1})}
    decided = {(s["field"], s["name_norm"]): s["status"] async for s in db[ndb.ENTITY_SUGGESTIONS].find(
        {"source_type": source_type, "source_id": source_id}, {"field": 1, "name_norm": 1, "status": 1})}
    rows = []
    for r in bt.resolve_item(index, organisations, locations):
        norm = normalize_alias(r.name)
        if r.entity_id in mentions or decided.get((r.field, norm), "open") != "open":
            action = ALREADY_DONE
        elif r.outcome == rs.ACTIVE:
            action = AUTO_LINK if written_exactly(r.name, r.matched_alias, text) else SUGGEST_NOT_WRITTEN
        elif r.outcome == rs.STAGED:
            action = SUGGEST_SWITCHED_OFF
        elif r.outcome == rs.UNKNOWN:
            action = NEW_NAME
        else:
            action = f"skip_{r.outcome}"
        status, snippet = bt.evidence(r, text) if r.entity_id else ("", "")
        rows.append({"source_type": source_type, "source_id": source_id, "title": item_title(source_type, doc)[:120],
                     "action": action, "field": r.field, "given_as": r.given_as or "", "name": r.name,
                     "entity": r.entity_name or "", "matched_alias": r.matched_alias or "",
                     "in_text": status, "snippet": snippet})
    return rows


async def apply_item(db, sources: ndb.ItemSources, source_type: str, doc: dict,
                     organisations: List[str], locations: List[str]) -> Dict[str, int]:
    """Suggest like a live item, auto-link the safe ones, and queue unknown names (the item is approved)."""
    source_id = item_id(doc)
    text = item_text(source_type, doc)
    counts = {"auto_linked": 0, "left_as_suggestion": 0}
    await lnk.suggest(db, source_type, source_id, organisations, locations, actor=ACTOR)
    open_suggestions = await db[ndb.ENTITY_SUGGESTIONS].find(
        {"source_type": source_type, "source_id": source_id, "status": "open", "entity_id": {"$ne": None}}).to_list(length=None)
    active = {e["_id"] async for e in db[ndb.ENTITIES].find(
        {"_id": {"$in": [s["entity_id"] for s in open_suggestions]}, "status": "active"}, {"_id": 1})}
    for s in open_suggestions:
        if s["entity_id"] in active and written_exactly(s["name"], s.get("matched_alias"), text):
            await lnk.link(db, sources, source_type, source_id, s["entity_id"], ACTOR,
                           suggestion_id=s["_id"], link_source=LINK_SOURCE)
            counts["auto_linked"] += 1
        else:
            counts["left_as_suggestion"] += 1
    await lnk.on_item_status(db, source_type, source_id, "approved")
    return counts


# --- Undo -------------------------------------------------------------------------------

@dataclass
class UndoReport:
    links: int = 0
    items: int = 0
    removed_by_reviewers_kept: int = 0
    rows: List[dict] = field(default_factory=list)


async def undo(db, apply: bool) -> UndoReport:
    """Removes every link the backfill made that is still linked, and reopens its suggestions.
    Links a reviewer later removed stay removed (their decision). Reviewers' own links are never touched."""
    report = UndoReport()
    report.removed_by_reviewers_kept = await db[ndb.ENTITY_MENTIONS].count_documents(
        {"link_source": LINK_SOURCE, "status": "removed"})
    mentions = await db[ndb.ENTITY_MENTIONS].find({"link_source": LINK_SOURCE, "status": "linked"}).to_list(length=None)
    report.links = len(mentions)
    items = sorted({(m["source_type"], m["source_id"]) for m in mentions})
    report.items = len(items)
    report.rows = [{"source_type": m["source_type"], "source_id": m["source_id"], "entity_id": m["entity_id"],
                    "title": m.get("title") or ""} for m in mentions]
    if not apply:
        return report
    for m in mentions:
        await db[ndb.ENTITY_MENTIONS].delete_one({"_id": m["_id"], "status": "linked", "link_source": LINK_SOURCE})
        await db[ndb.ENTITY_SUGGESTIONS].update_many(
            {"source_type": m["source_type"], "source_id": m["source_id"], "entity_id": m["entity_id"],
             "status": "linked", "decided_by": ACTOR},
            {"$set": {"status": "open", "decided_at": None, "decided_by": None}})
        await ent.audit(db, UNDO_ACTOR, "unlink", m["entity_id"],
                        {"source_type": m["source_type"], "source_id": m["source_id"]}, None)
    for source_type, source_id in items:
        await refresh_item_locations(db, source_type, source_id)
    return report

