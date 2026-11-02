"""
LLM Observability for the backoffice: figures read from Phoenix (never prompts or answers).

The backoffice cannot reach Phoenix (it runs inside the server's network), so NewsAgent asks it and returns
a summary per service (Phoenix project), per model, per kind of job, and the latest failed jobs. No AI call is
made; answers are cached for a minute so refreshing the page does not load Phoenix.

Per-job figures work because every step of a job carries the job's name (tracing.job_span sets
metadata["job"]), so Phoenix can add up cost and tokens with one filter per job.

Detail, only when a reviewer asks for it (never on page load): job_runs() lists a kind of job's latest runs,
trace_steps() one run's steps (name, time, status, error, model, tokens, cost). Prompt and answer text is
never returned: it stays in Phoenix until the backoffice's calls go through a server route (security slice).
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from src.configs.settings import settings

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
FAILURES_SHOWN = 20
RUNS_SHOWN = 20
MAX_STEPS = 500
DETAIL_CACHE_SIZE = 200

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
    "article_match_vector": ("Pending-article vectors", "newsagent"),
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
    "signal_vectors": ("Signal vectors (older signals)", "social-scraper"),
}
# Phoenix allows 50 named fields per request: 5 per job
JOBS_PER_REQUEST = 9

SERVICE_LABELS = {"newsagent": "NewsAgent", "social-scraper": "Social scraper"}

_cache: Dict[int, Tuple[float, dict]] = {}
_detail_cache: Dict[tuple, Tuple[float, dict]] = {}


class PhoenixUnavailable(RuntimeError):
    pass


class NotFound(LookupError):
    """An unknown kind of job or trace (the endpoint answers 404)."""


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


def _reported(project_name: str) -> bool:
    """A Phoenix project the page reports on: those in PHOENIX_REPORT_PROJECTS, else all but "default"."""
    wanted = [p.strip() for p in (settings.PHOENIX_REPORT_PROJECTS or "").split(",") if p.strip()]
    return project_name in wanted if wanted else project_name != "default"


async def _report_projects(client: httpx.AsyncClient) -> List[dict]:
    listed = [e["node"] for e in (await _graphql(client, PROJECTS_QUERY, {}))["projects"]["edges"]]
    return [p for p in listed if _reported(p["name"])]


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
            for p in await _report_projects(client):
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


# --- Detail, fetched only when a reviewer opens a job or one of its runs --------------------------------

RUNS_QUERY = """
query Runs($range: TimeRange!, $project: ID!, $filter: String!, $first: Int!) {
  node(id: $project) { ... on Project {
    spans(first: $first, timeRange: $range, filterCondition: $filter, sort: {col: startTime, dir: desc}) {
      edges { node {
        startTime latencyMs statusCode statusMessage cumulativeTokenCountTotal
        context { traceId }
        output { value }
        trace { numSpans costSummary { total { cost } } }
      } }
    }
  } }
}"""

TRACE_QUERY = """
query Trace($traceId: String!) {
  getTraceByOtelId(traceId: $traceId) {
    traceId startTime latencyMs numSpans
    project { name }
    costSummary { prompt { cost tokens } completion { cost tokens } total { cost tokens } }
    spans(first: %d) { edges { node {
      spanId parentId name spanKind statusCode statusMessage startTime latencyMs
      tokenCountPrompt tokenCountCompletion costSummary { total { cost } } attributes
    } } }
  }
}""" % MAX_STEPS

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def _cached(key: tuple) -> Optional[dict]:
    hit = _detail_cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < CACHE_SECONDS else None


def _remember(key: tuple, value: dict) -> dict:
    if len(_detail_cache) >= DETAIL_CACHE_SIZE:
        _detail_cache.pop(min(_detail_cache, key=lambda k: _detail_cache[k][0]))  # the oldest answer
    _detail_cache[key] = (time.monotonic(), value)
    return value


def _status(code: Optional[str]) -> str:
    return "failed" if code == "ERROR" else "ok"


def _outcome(output: Optional[dict]) -> Optional[str]:
    """The job's own one-word result from the top of its trace (completed, retried, handed back, skipped…)."""
    try:
        value = json.loads((output or {}).get("value") or "")
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict):
        outcome = value.get("outcome") or value.get("status")
        return str(outcome)[:60] if outcome else None
    return None


def _model(attributes: Optional[str]) -> Optional[str]:
    """Only the model's name from a step's details (the rest, prompts and answers included, is dropped)."""
    try:
        attrs = json.loads(attributes or "{}")
    except ValueError:
        return None
    for group in ("llm", "embedding"):
        part = attrs.get(group)
        if isinstance(part, dict) and part.get("model_name"):
            return str(part["model_name"])
        if attrs.get(f"{group}.model_name"):
            return str(attrs[f"{group}.model_name"])
    return None


def _unavailable(result: dict) -> Optional[dict]:
    if not phoenix_url():
        result["message"] = "Tracing is not switched on: PHOENIX_COLLECTOR_ENDPOINT is not set on NewsAgent."
        return result
    return None


async def job_runs(job: str, days: int, limit: int = RUNS_SHOWN, now: Optional[datetime] = None) -> dict:
    """A kind of job's latest runs (newest first), each with its time, cost, tokens, status and number of steps."""
    if job not in JOBS:
        raise NotFound(f"Unknown job: {job}")
    label, service = JOBS[job]
    cached = _cached(("runs", job, days, limit)) if now is None else None
    if cached:
        return cached
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    result = {"available": False, "message": None, "job": job, "label": label, "days": days, "runs": []}
    if _unavailable(result):
        return result
    variables = {"range": {"start": since.isoformat(), "end": until.isoformat()},
                 "filter": f"name == {_q(job)} and parent_id is None", "first": limit}
    runs = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            for p in await _report_projects(client):
                if p["name"].partition("/")[0] != service:
                    continue
                node = (await _graphql(client, RUNS_QUERY, {**variables, "project": p["id"]}))["node"]
                for edge in node["spans"]["edges"]:
                    run = edge["node"]
                    trace = run.get("trace") or {}
                    runs.append({"trace_id": run["context"]["traceId"], "service": service_label(p["name"]),
                                 "at": run["startTime"], "duration_ms": run.get("latencyMs"),
                                 "status": _status(run.get("statusCode")), "outcome": _outcome(run.get("output")),
                                 "error": (run.get("statusMessage") or "")[:300] or None,
                                 "tokens": int(run.get("cumulativeTokenCountTotal") or 0),
                                 "cost_usd": _cost(trace.get("costSummary"))[0], "steps": trace.get("numSpans")})
    except PhoenixUnavailable as e:
        logger.warning(f"[OBSERVABILITY] {e}")
        result["message"] = str(e)
        return result
    result["runs"] = sorted(runs, key=lambda r: r["at"], reverse=True)[:limit]
    result["available"] = True
    return _remember(("runs", job, days, limit), result) if now is None else result


async def trace_steps(trace_id: str) -> dict:
    """One run's steps in order, each with its depth (step inside step), start (ms after the run started),
    time, status, error, model, tokens and cost. No prompt or answer text."""
    trace_id = (trace_id or "").strip().lower()
    if not TRACE_ID.match(trace_id):
        raise NotFound("A trace id is 32 letters and digits (0-9, a-f)")
    cached = _cached(("trace", trace_id))
    if cached:
        return cached
    result = {"available": False, "message": None, "trace_id": trace_id}
    if _unavailable(result):
        return result
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            trace = (await _graphql(client, TRACE_QUERY, {"traceId": trace_id})).get("getTraceByOtelId")
    except PhoenixUnavailable as e:
        logger.warning(f"[OBSERVABILITY] {e}")
        result["message"] = str(e)
        return result
    if not trace or not _reported((trace.get("project") or {}).get("name") or ""):
        raise NotFound("No such trace (it may be older than Phoenix keeps traces)")

    spans = [e["node"] for e in trace["spans"]["edges"]]
    by_id = {s["spanId"]: s for s in spans}

    def depth(span: dict) -> int:
        level, parent = 0, span.get("parentId")
        while parent in by_id and level < 50:
            level, parent = level + 1, by_id[parent].get("parentId")
        return level

    start = datetime.fromisoformat(trace["startTime"])
    steps = []
    for s in sorted(spans, key=lambda s: s["startTime"]):
        prompt, completion = int(s.get("tokenCountPrompt") or 0), int(s.get("tokenCountCompletion") or 0)
        steps.append({"span_id": s["spanId"], "parent_id": s.get("parentId"), "depth": depth(s), "name": s["name"],
                      "kind": (s.get("spanKind") or "").lower(), "status": _status(s.get("statusCode")),
                      "error": (s.get("statusMessage") or "")[:300] or None,
                      "start_ms": round((datetime.fromisoformat(s["startTime"]) - start).total_seconds() * 1000),
                      "duration_ms": s.get("latencyMs"), "model": _model(s.get("attributes")),
                      "tokens_prompt": prompt, "tokens_completion": completion,
                      "cost_usd": _cost(s.get("costSummary"))[0]})
    root = next((s for s in steps if not s["parent_id"]), steps[0] if steps else None)
    _, prompt_tokens = _cost(trace.get("costSummary"), "prompt")
    _, completion_tokens = _cost(trace.get("costSummary"), "completion")
    result.update({
        "available": True, "service": service_label(trace["project"]["name"]),
        "job": root["name"] if root else None, "label": JOBS.get(root["name"], (root["name"], ""))[0] if root else None,
        "at": trace["startTime"], "duration_ms": trace.get("latencyMs"),
        "status": "failed" if any(s["status"] == "failed" for s in steps if not s["parent_id"]) else "ok",
        "cost_usd": _cost(trace.get("costSummary"))[0], "tokens_prompt": prompt_tokens,
        "tokens_completion": completion_tokens, "steps_total": trace.get("numSpans") or len(steps),
        "steps": steps,
    })
    return _remember(("trace", trace_id), result)
