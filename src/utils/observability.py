"""
LLM Observability for the backoffice: totals read from Phoenix (never prompts or answers).

The backoffice cannot reach Phoenix (it runs inside the server's network), so NewsAgent asks it and returns
a summary per service (Phoenix project), per model, per kind of job, and the latest failed jobs. No AI call is
made; answers are cached for a minute so refreshing the page does not load Phoenix.

Per-job figures work because every step of a job carries the job's name (tracing.job_span sets
metadata["job"]), so Phoenix can add up cost and tokens with one filter per job.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from src.configs.settings import settings

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
FAILURES_SHOWN = 20

# The jobs the services trace (tracing.job_span names), with how the backoffice shows them and which
# service (Phoenix project prefix) runs them
JOBS: Dict[str, Tuple[str, str]] = {
    "article_pipeline": ("Articles processed", "newsagent"),
    "compose_compose": ("Newsroom compose runs", "newsagent"),
    "compose_extract_signal": ("Signals extracted from articles", "newsagent"),
    "article_translate": ("Article edits: Translate", "newsagent"),
    "article_edit": ("Article edits: Save", "newsagent"),
    "article_vectorize": ("Approved-article embeddings", "newsagent"),
    "similarity_search": ("Similar Articles searches", "newsagent"),
    "backfill_countries": ("Backfill: countries", "newsagent"),
    "backfill_social": ("Backfill: social captions", "newsagent"),
    "backfill_names": ("Backfill: Newsroom names", "newsagent"),
    "backfill_trial_names": ("Backfill trial: names", "newsagent"),
    "real_estate_trial": ("Real-estate check trial", "newsagent"),
    "country_check_old": ("Country check (old prompt)", "newsagent"),
    "country_check_new": ("Country check (new prompt)", "newsagent"),
    "grok_scout": ("Grok Scout runs", "social-scraper"),
    "signal_synthesis": ("Social signal synthesis runs", "social-scraper"),
    "post_analysis": ("Social posts analysed", "social-scraper"),
    "signal_refine": ("Signal Refine", "social-scraper"),
    "signal_topics": ("Topic tagging (added signals)", "social-scraper"),
}
# Phoenix allows 50 named fields per request: 5 per job
JOBS_PER_REQUEST = 9

SERVICE_LABELS = {"newsagent": "NewsAgent", "social-scraper": "Social scraper"}

_cache: Dict[int, Tuple[float, dict]] = {}


class PhoenixUnavailable(RuntimeError):
    pass


def phoenix_url() -> Optional[str]:
    """Where to read Phoenix: PHOENIX_URL, or the address traces are sent to without /v1/traces."""
    url = (settings.PHOENIX_URL or "").strip() or (settings.PHOENIX_COLLECTOR_ENDPOINT or "").strip()
    if not url:
        return None
    url = url.rstrip("/")
    return url[: -len("/v1/traces")] if url.endswith("/v1/traces") else url


def service_label(project: str) -> str:
    prefix, _, environment = project.partition("/")
    label = SERVICE_LABELS.get(prefix, prefix)
    return f"{label} ({environment})" if environment else label


def _q(text: str) -> str:
    """A value inside a Phoenix filter (written in Python syntax)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _graphql(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.PHOENIX_API_KEY:
        headers["Authorization"] = f"Bearer {settings.PHOENIX_API_KEY}"
    try:
        response = await client.post(f"{phoenix_url()}/graphql", json={"query": query, "variables": variables},
                                     headers=headers)
    except httpx.HTTPError as e:
        raise PhoenixUnavailable(f"Phoenix could not be reached: {e}") from e
    if response.status_code >= 400:
        raise PhoenixUnavailable(f"Phoenix answered {response.status_code}")
    body = response.json()
    if body.get("errors"):
        raise PhoenixUnavailable(f"Phoenix refused the query: {body['errors'][0].get('message')}")
    return body["data"]


def _gql(text: str) -> str:
    """A GraphQL string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


PROJECTS_QUERY = "{ projects(first: 50) { edges { node { id name } } } }"

PROJECT_QUERY = """
query Project($range: TimeRange!, $project: ID!) {
  node(id: $project) { ... on Project {
    jobs: traceCount(timeRange: $range)
    failed: recordCount(timeRange: $range, filterCondition: "parent_id is None and status_code == \\"ERROR\\"")
    calls: recordCount(timeRange: $range, filterCondition: "span_kind == \\"LLM\\"")
    cost: costSummary(timeRange: $range) { prompt { cost tokens } completion { cost tokens } total { cost tokens } }
    p50: latencyMsQuantile(probability: 0.5, timeRange: $range)
    p90: latencyMsQuantile(probability: 0.9, timeRange: $range)
    models: topModelsByCost(timeRange: $range) {
      name
      costSummary(projectId: $project, timeRange: $range) { prompt { cost tokens } completion { cost tokens } total { cost } }
    }
    failures: spans(first: %d, timeRange: $range, filterCondition: "parent_id is None and status_code == \\"ERROR\\"",
                    sort: {col: startTime, dir: desc}) {
      edges { node { name startTime statusMessage context { traceId } } }
    }
  } }
}""" % FAILURES_SHOWN


def _jobs_query(names: List[str]) -> str:
    """Count, failures, middle time, AI calls and cost of each job (every step inside carries metadata["job"])."""
    fields = []
    for i, name in enumerate(names):
        root = f'name == {_q(name)} and parent_id is None'
        inside = f'metadata["job"] == {_q(name)}'
        fields.append(f"""
      j{i}_count: recordCount(timeRange: $range, filterCondition: {_gql(root)})
      j{i}_failed: recordCount(timeRange: $range, filterCondition: {_gql(root + ' and status_code == "ERROR"')})
      j{i}_p50: spanLatencyMsQuantile(probability: 0.5, timeRange: $range, filterCondition: {_gql(root)})
      j{i}_calls: recordCount(timeRange: $range, filterCondition: {_gql(inside + ' and span_kind == "LLM"')})
      j{i}_cost: costSummary(timeRange: $range, filterCondition: {_gql(inside)}) {{ total {{ cost tokens }} }}""")
    return "query Jobs($range: TimeRange!, $project: ID!) {\n  node(id: $project) { ... on Project {%s\n  } }\n}" % "".join(fields)


def _jobs_for(project: str) -> List[str]:
    prefix = project.partition("/")[0]
    own = [name for name, (_, service) in JOBS.items() if service == prefix]
    return own or list(JOBS)


def _cost(summary: Optional[dict], part: str = "total") -> Tuple[float, int]:
    piece = (summary or {}).get(part) or {}
    return float(piece.get("cost") or 0), int(piece.get("tokens") or 0)


async def summary(days: int, now: Optional[datetime] = None) -> dict:
    """The page's figures for the last `days` days (cached for a minute)."""
    cached = _cache.get(days)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS and now is None:
        return cached[1]
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    result = {"available": False, "message": None, "since": since, "until": until, "days": days,
              "totals": None, "services": [], "models": [], "jobs": [], "failures": []}
    if not phoenix_url():
        result["message"] = "Tracing is not switched on: PHOENIX_COLLECTOR_ENDPOINT is not set on NewsAgent."
        return result

    time_range = {"start": since.isoformat(), "end": until.isoformat()}
    projects = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            listed = [e["node"] for e in (await _graphql(client, PROJECTS_QUERY, {}))["projects"]["edges"]]
            wanted = [p.strip() for p in (settings.PHOENIX_REPORT_PROJECTS or "").split(",") if p.strip()]
            for p in listed:
                if (p["name"] not in wanted) if wanted else p["name"] == "default":
                    continue
                variables = {"range": time_range, "project": p["id"]}
                node = (await _graphql(client, PROJECT_QUERY, variables))["node"]
                node["name"], node["job_figures"] = p["name"], {}
                names = _jobs_for(p["name"]) if node.get("jobs") else []
                for start_at in range(0, len(names), JOBS_PER_REQUEST):
                    batch = names[start_at:start_at + JOBS_PER_REQUEST]
                    figures = (await _graphql(client, _jobs_query(batch), variables))["node"]
                    for i, name in enumerate(batch):
                        node["job_figures"][name] = {k[len(f"j{i}_"):]: v for k, v in figures.items() if k.startswith(f"j{i}_")}
                projects.append(node)
    except PhoenixUnavailable as e:
        logger.warning(f"[OBSERVABILITY] {e}")
        result["message"] = str(e)
        return result

    totals = {"jobs": 0, "failed_jobs": 0, "ai_calls": 0, "tokens_prompt": 0, "tokens_completion": 0, "cost_usd": 0.0}
    for p in projects:
        _, prompt_tokens = _cost(p["cost"], "prompt")
        _, completion_tokens = _cost(p["cost"], "completion")
        total_cost, _ = _cost(p["cost"])
        service = {"project": p["name"], "label": service_label(p["name"]), "jobs": p["jobs"] or 0,
                   "failed_jobs": p["failed"] or 0, "ai_calls": p["calls"] or 0, "tokens_prompt": prompt_tokens,
                   "tokens_completion": completion_tokens, "cost_usd": total_cost,
                   "p50_ms": p["p50"], "p90_ms": p["p90"]}
        result["services"].append(service)
        for key in totals:
            totals[key] += service[key]
        for model in p.get("models") or []:
            _, m_prompt_tokens = _cost(model["costSummary"], "prompt")
            _, m_completion_tokens = _cost(model["costSummary"], "completion")
            m_total, _ = _cost(model["costSummary"])
            result["models"].append({"model": model["name"], "service": service["label"], "cost_usd": m_total,
                                     "tokens_prompt": m_prompt_tokens, "tokens_completion": m_completion_tokens})
        for name, figures in p["job_figures"].items():
            count = figures.get("count") or 0
            if not count:
                continue
            job_cost, job_tokens = _cost(figures.get("cost"))
            result["jobs"].append({"job": name, "label": JOBS.get(name, (name, ""))[0], "service": service["label"],
                                   "count": count, "failed": figures.get("failed") or 0,
                                   "ai_calls": figures.get("calls") or 0, "tokens": job_tokens, "cost_usd": job_cost,
                                   "avg_cost_usd": job_cost / count, "p50_ms": figures.get("p50")})
        for edge in p["failures"]["edges"]:
            node = edge["node"]
            result["failures"].append({"job": node["name"], "label": JOBS.get(node["name"], (node["name"], ""))[0],
                                       "service": service["label"], "at": node["startTime"],
                                       "error": (node.get("statusMessage") or "")[:300],
                                       "trace_id": node["context"]["traceId"]})
    result["failures"] = sorted(result["failures"], key=lambda f: f["at"], reverse=True)[:FAILURES_SHOWN]
    result["models"].sort(key=lambda m: m["cost_usd"], reverse=True)
    result["jobs"].sort(key=lambda j: j["cost_usd"], reverse=True)
    result["totals"] = totals
    result["available"] = True
    if now is None:
        _cache[days] = (time.monotonic(), result)
    return result
