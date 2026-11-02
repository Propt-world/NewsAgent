"""
Backfill trial (step 8): what the backfill would link on old approved articles,
measured before anything is written.

- Names are matched with the same rules as live suggestions (resolve.py), against
  the starting lists read straight from the seed files. No database is used.
- "Would auto-link" = an exact alias of a switched-on entity. Staged entities,
  ambiguous names, ignored strings and handles never link.
- Pass "labels": the names in the Claude-labelled workbook (free).
- Pass "model": the real name step (extract_country with names, the init_db
  prompts) on a sample, compared with the labels pass. Costs a little; capped.
- Every would-be link records whether the matched name appears in the article
  text, so made-up names and lower-case words ("a 30% stake") stand out.
"""
import csv
import random
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from src.newsroom import resolve as rs
from src.newsroom.normalize import normalize_alias
from src.newsroom.seed import SeedPlan

# Workbook cells that mean "nothing named"
EMPTY_CELLS = {"", "not stated", "not applicable", "none", "n/a", "na", "-", "unknown", "not specified"}
SNIPPET_CHARS = 80

# in_text values
EXACT_CASE = "exact_case"
OTHER_CASE = "other_case_only"
ABSENT = "absent"


def split_cell(value) -> List[str]:
    """A workbook cell such as "Aldar, Emaar" -> ["Aldar", "Emaar"]; placeholders -> []."""
    names = []
    for part in str(value or "").split(","):
        part = part.strip()
        if part.casefold() not in EMPTY_CELLS:
            names.append(part)
    return names


def label_names(row: dict) -> Tuple[List[str], List[str]]:
    """Workbook row -> (organisation names, location names), split the way the pipeline splits
    its own names (pipeline.split_names: companies + projects are organisations)."""
    organisations = split_cell(row.get("Company / Developer")) + split_cell(row.get("Project"))
    return organisations, split_cell(row.get("Location"))


def model_names(newsroom_names: Optional[dict]) -> Tuple[List[str], List[str]]:
    from src.newsroom.pipeline import split_names
    return split_names(newsroom_names or {})


@dataclass
class NameResult:
    field: str
    name: str
    outcome: str
    entity_id: Optional[str] = None
    entity_name: Optional[str] = None
    entity_status: Optional[str] = None
    matched_alias: Optional[str] = None
    # The list the name was given in, when it was found in the other one (e.g. a project name that is a place)
    given_as: Optional[str] = None


class ListIndex:
    """The starting lists in memory, looked up exactly like entity_aliases / entities / entity_ignore."""

    def __init__(self, entities: Iterable[dict], aliases: Iterable[dict], ignored_keys: Iterable[str]):
        self.entities = {e["_id"]: e for e in entities}
        self.ignored = set(ignored_keys)
        self.aliases: Dict[Tuple[str, str], List[dict]] = {}
        for alias in aliases:
            self.aliases.setdefault((alias["kind"], alias["alias_norm"]), []).append(alias)

    @classmethod
    def from_plan(cls, plan: SeedPlan) -> "ListIndex":
        return cls(plan.entities, plan.aliases, [i["string_key"] for i in plan.ignore])

    def _resolve_in(self, kind: str, name: str) -> rs.Resolution:
        aliases = self.aliases.get((kind, normalize_alias(name)), [])
        exact = rs.exact_alias(aliases)
        return rs.resolve(name, self.ignored, aliases, self.entities.get(exact["entity_id"]) if exact else None)

    def resolve(self, field: str, name: str) -> NameResult:
        """Same as links.suggest: the name's own list first, then the other list (exact alias only)."""
        res, kind = self._resolve_in(field, name), field
        if res.outcome == rs.UNKNOWN:
            other = self._resolve_in(rs.other_kind(field), name)
            if other.outcome in rs.OTHER_LIST_OUTCOMES:
                res, kind = other, rs.other_kind(field)
        result = NameResult(field=kind, name=name, outcome=res.outcome, given_as=field if kind != field else None)
        if res.entity is not None:
            result.entity_id = res.entity["_id"]
            result.entity_name = res.entity["canonical_name"]
            result.entity_status = res.entity["status"]
        if res.alias is not None:
            result.matched_alias = res.alias["alias"]
        return result


def resolve_item(index: ListIndex, organisations: List[str], locations: List[str]) -> List[NameResult]:
    """One result per distinct (field, normalised name), like links.suggest."""
    results, seen = [], set()
    for field_name, values in (("organisation", organisations), ("location", locations)):
        for name in values or []:
            name = (name or "").strip()
            norm = normalize_alias(name)
            if not norm or (field_name, norm) in seen:
                continue
            seen.add((field_name, norm))
            results.append(index.resolve(field_name, name))
    return results


def auto_links(results: List[NameResult]) -> Dict[str, NameResult]:
    """The links the backfill would write under option A: exact aliases of switched-on entities."""
    links = {}
    for r in results:
        if r.outcome == rs.ACTIVE and r.entity_id not in links:
            links[r.entity_id] = r
    return links


def article_text(article: dict) -> str:
    final = article.get("final_output") or {}
    return "\n".join(str(final.get(k) or "") for k in ("title", "summary", "content"))


def name_in_text(name: str, text: str) -> Tuple[str, str]:
    """(EXACT_CASE | OTHER_CASE | ABSENT, snippet around the first hit). Whole words only."""
    if not name:
        return ABSENT, ""
    pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
    hit = re.search(pattern, text)
    status = EXACT_CASE
    if hit is None:
        hit = re.search(pattern, text, re.IGNORECASE)
        status = OTHER_CASE
    if hit is None:
        return ABSENT, ""
    start, end = max(0, hit.start() - SNIPPET_CHARS), min(len(text), hit.end() + SNIPPET_CHARS)
    return status, " ".join(text[start:end].split())


def evidence(link: NameResult, text: str) -> Tuple[str, str]:
    """Where the found name (or, failing that, the matched alias) appears in the article."""
    status, snippet = name_in_text(link.name, text)
    if status == ABSENT and link.matched_alias and link.matched_alias != link.name:
        status, snippet = name_in_text(link.matched_alias, text)
    return status, snippet


def read_labels(path: Path) -> List[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if (r.get("Article ID") or "").strip()]


def sample_ids(ids: List[str], size: Optional[int], seed: int) -> List[str]:
    """A repeatable random sample (the same seed gives the same articles, so cached results are reused)."""
    ids = sorted(ids)
    if size is None or size >= len(ids):
        return ids
    return sorted(random.Random(seed).sample(ids, size))


@dataclass
class Comparison:
    agree: List[str] = field(default_factory=list)
    model_only: List[str] = field(default_factory=list)
    labels_only: List[str] = field(default_factory=list)


def compare(model_links: Dict[str, NameResult], label_links: Dict[str, NameResult]) -> Comparison:
    return Comparison(
        agree=sorted(set(model_links) & set(label_links)),
        model_only=sorted(set(model_links) - set(label_links)),
        labels_only=sorted(set(label_links) - set(model_links)),
    )


def result_row(article_id: str, title: str, link: NameResult, text: str, **extra) -> dict:
    status, snippet = evidence(link, text)
    row = {"article_id": article_id, "title": title, **extra, **asdict(link), "in_text": status, "snippet": snippet}
    return row
