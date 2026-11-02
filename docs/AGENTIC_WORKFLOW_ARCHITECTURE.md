# NewsAgent — Agentic Workflow Architecture (Portable Spec)

> Purpose: this document describes the architecture, state management, and
> operational patterns of the NewsAgent pipeline **abstractly enough to be
> reimplemented in a different tech stack**. Anywhere a NewsAgent-specific
> name is used (e.g. "why_this_matters", "SearXNG"), the accompanying prose
> explains the *general pattern* it's an instance of, so an agent building a
> similar system on a different stack (different language, different
> orchestration library, different domain) can follow the pattern rather
> than copy the specifics.

---

## 1. Architectural pattern

**Linear pipeline of typed, stateless transform steps over one shared state
object, with a single bounded conditional retry loop.** This is *not* an
autonomous/agentic multi-tool-call loop (no ReAct, no free-form tool
selection by an LLM) — it's a deterministic DAG where each node is a plain
function `(state) -> state`, some of which happen to call an LLM internally.

Concretely here: **LangGraph** (`StateGraph`) is used as the DAG runner, but
the pattern does not depend on LangGraph. The three ingredients to
reproduce in any stack are:

1. **A single immutable state schema** that every step receives and returns
   an updated copy of (never mutate in place).
2. **A graph/orchestrator object** that wires steps together as
   `add_node` + `add_edge`, with `add_conditional_edges` for the one
   branch point (retry-or-continue).
2. **A process split**: a lightweight producer that accepts work and a
   separate worker process that actually runs the graph, decoupled by a
   queue — so the API layer never blocks on LLM/scrape latency.

If porting to e.g. Node/TypeScript, this maps naturally to: a state
interface/type, a hand-rolled or library-based (e.g. a small `runGraph`
function, or a tool like Mastra/Temporal/BullMQ-workflow) sequential
executor, and a queue (BullMQ/SQS/etc.) between an HTTP layer and a worker.

### Entry points (3 independent processes, one shared graph definition)

| Process | Role | Framework | Never does |
|---|---|---|---|
| **API** (`src/main.py`) | Accepts job requests, pushes to queue; admin CRUD for prompts/categories | FastAPI | Never invokes the graph directly |
| **Worker** (`src/worker.py`) | Pops jobs off queue, builds graph once at startup, runs `graph.ainvoke(state)` per job | FastAPI-less, plain asyncio loop | Never serves HTTP |
| **Scheduler** (`src/scheduler/main.py`) | Cron-like discovery of new work items (scrapes listing pages for new URLs), POSTs them to the API; also receives the final webhook and persists results | FastAPI + APScheduler | Never touches the graph itself |

**Why this split matters (carry this to the new project):** the thing that
takes a long time (scraping + multiple LLM calls, easily 30-90s) is
isolated in a worker that only talks to a queue. The public-facing API
stays fast and stateless. The scheduler is a third, fully independent
concern (sourcing work) — don't conflate "what feeds the pipeline" with
"what runs the pipeline."

---

## 2. State management

### 2.1 The state object

One flat Pydantic model, `MainWorkflowState`, is the *only* thing passed
between steps. Every field is optional except the seed input, because
different steps populate different fields as the pipeline progresses:

```python
class MainWorkflowState(BaseModel):
    source_url: str                              # seed input, set once at job creation
    cleaned_article_text: Optional[str] = None    # populated by step 2
    cleaned_article_html: Optional[str] = None
    news_article: Optional[ArticleModel] = None   # the "payload" — see 2.2
    active_prompts: Optional[AgentPromptsModel] = None  # loaded once at step 1
    category_mapping: Dict[str, str] = Field(default_factory=dict)
    validation_count: int = 0                     # retry-loop counter
    validation_result: Optional[ValidationResultModel] = None
    summary_attempts: List[SummaryAttemptModel] = Field(default_factory=list)
    other_sources: List[Dict] = Field(default_factory=list)
    search_query_data: Optional[SearchQueryModel] = None
    max_retries: int = 3
    error_message: Optional[str] = None           # fail-fast flag, see 2.3
```

**Pattern to carry over:** split the state into (a) top-level pipeline
bookkeeping (retry counters, error flag, config loaded once) and (b) one
nested "payload" object that accumulates the actual output fields. Don't
flatten everything into one object — the payload model is what eventually
gets serialized out (webhook, DB row, API response); the bookkeeping fields
are pipeline-internal and get discarded.

### 2.2 The payload model

`ArticleModel` is progressively enriched — each step only ever adds/updates
its own field(s), never touches fields it didn't produce:

```
title, content, source_title, top_image, summary, reading_time,
published_date, author, category, category_ids, countries,
embedded_links: List[EmbeddedLinkModel],
social_media: SocialCaptionModel,
why_this_matters: WhyThisMattersModel,   # domain-specific enrichment step
seo: SeoMetadataModel,
title_ar, summary_ar, content_ar, reading_time_ar   # localized variants
```

### 2.3 Immutability convention

Steps never mutate state in place. Every step ends with:

```python
return state.model_copy(update={"news_article": updated_article})
```

...where `updated_article` is itself built via `news_article.model_copy(update={...})`
for the one field that step owns. This is a **manual convention**, not
enforced by the orchestrator (this graph has no reducer/merge functions —
whichever node returns last simply replaces the state wholesale). If your
new stack's orchestrator supports enforced immutability or reducers, prefer
that; otherwise, enforce the "always return a copy, never mutate" rule by
convention/lint and code review.

### 2.4 Fail-fast short-circuit (not exceptions)

Almost every step's first line is a guard:

```python
async def some_node(state: MainWorkflowState) -> MainWorkflowState:
    if state.error_message:
        return state          # pass through unchanged, do no work
    try:
        ...
    except Exception as e:
        return state.model_copy(update={"error_message": str(e)})
```

This means a failure doesn't raise/crash the graph — it sets a flag that
every subsequent step checks and no-ops on, so the graph always runs to
completion and the *last* state is inspected by the caller (`worker.py`)
for `error_message`. **Non-critical** steps (things that enrich but aren't
essential to a usable output — link relevance scoring, translation, social
captions, the final webhook) instead swallow their own internal errors and
return state unchanged, so a failure in an optional enrichment doesn't kill
an otherwise-good article. Only **critical-path** steps (scrape, summary
generation/validation) propagate `error_message` and effectively halt
useful further work.

**Carry over:** classify every step as critical-path vs. best-effort up
front, and pick the failure behavior per step accordingly — don't let one
brittle enrichment call (e.g. translation) take down a pipeline that
otherwise produced a usable result.

### 2.5 The one conditional loop: generate → validate → retry

This is the only branch point in the whole graph:

```
generate_summary → validate_summary → [conditional]
                         ├─ "regenerate" (score below threshold, retries left) → back to generate_summary
                         └─ "end_loop" (passed, or retries exhausted)          → select_best_summary
```

- `generate_summary` uses a *different* prompt on retry (`summary_retry_user`
  vs `summary_initial_user`), which is given the previous attempt's
  validator feedback so the retry is informed, not blind.
- Every attempt (summary text + validator scores) is appended to
  `summary_attempts: List[SummaryAttemptModel]` rather than overwritten.
- The loop is bounded by `state.max_retries` (default 3) checked against
  `state.validation_count` inside the conditional-edge function — **not**
  inside the generate/validate nodes themselves. Keep the loop-termination
  logic in one place (the edge/router function), not scattered.
- After the loop exits (by pass or by exhaustion), a separate
  `select_best_summary` step picks the highest-scoring attempt from
  `summary_attempts` as the final answer — so even an exhausted-retries
  exit still yields the *best available* result, not the *last* one.

**Generalizable pattern — "generate/critique/retry-with-feedback/pick-best":**
1. LLM call A produces a candidate.
2. LLM call B (a different prompt, ideally a stricter/critic-style one)
   scores the candidate on named sub-criteria (here: semantic_score,
   tone_score, format_check_passed) and returns structured output, not
   free text.
3. A pure router function decides regenerate-vs-continue from the score +
   an attempt counter, with a hard cap.
4. On regenerate, the critic's feedback is fed back into the generation
   prompt.
5. All attempts are kept, and the best-scoring one wins regardless of which
   loop iteration it came from.

This pattern is reusable for *any* "make sure the LLM output is good
enough" need in the new project — swap in whatever schema/criteria matter
for that domain.

### 2.6 Persistence — deliberately NOT via orchestrator checkpointing

The graph is compiled **without** a checkpointer — state exists only for
the duration of one `ainvoke()` call, in process memory. Durability is
handled by three separate, purpose-built mechanisms instead:

| Concern | Mechanism |
|---|---|
| Job lifecycle / status polling | Redis hash `job:{id}` with `status` ∈ {queued, processing, completed, failed, crashed}, `result` (JSON), `error`; TTL 24h |
| Work queue | Redis list, `BLPOP` consumer loop in the worker; a **dead-letter list** for failed/crashed jobs, with explicit requeue endpoints |
| Final durable output | The *last* pipeline step POSTs the completed payload to a webhook URL owned by a different service, which writes it to the real database |
| Config that must survive restarts and be editable without redeploying | Loaded from the database **at the start of every run** (see §3) — never baked into code |

**Carry over:** don't reach for the orchestration library's built-in
checkpointing unless you actually need mid-pipeline resume/replay. A
queue + status hash + terminal webhook is simpler, is infrastructure you
likely already have, and cleanly separates "is this job done" (cheap,
frequent polling) from "what's the full result" (fetched once, from the
real datastore, on completion).

---

## 3. Prompts as external, database-backed config

This is one of the more important patterns to replicate.

- Prompts are **not string constants in code**. Old code that did this
  (`src/prompts/*.py`) is dead/unused — every node instead reads from
  `state.active_prompts`, a Pydantic model with **one field per named
  prompt** (`summary_system`, `summary_retry_user`, `validation_system`,
  `content_enrichment_user`, etc. — 21 total).
- A dedicated first step, `load_agent_configuration`, runs once at the
  start of every job: it queries the prompts collection for
  `status == "active"` and the required names, and **fails the whole job
  immediately (schema validation error) if any required prompt is
  missing** — this makes "someone forgot to activate a prompt" loud and
  early rather than a silent bad LLM call three steps later.
- Prompts have a lifecycle: `active` / `draft` / `archived` (an enum), a
  `version`, and free-text `input_variables` documentation — so prompts can
  be iterated on and rolled back without a code deploy, and multiple
  drafts can exist without affecting production.
- Each step formats its prompt template with the variables it has in scope
  (e.g. `title`, `summary`, `categories`, a truncated content excerpt) and
  sends `[("system", ...), ("user", formatted)]` to the model.
- A seed script (`init_db.py`) is the source of truth for the *initial*
  prompt set and supports an idempotent "upsert by name" mode vs. a
  destructive "fresh" mode requiring an explicit confirmation flag.
- There's a maintained catalog doc (`docs/prompt_catalog.md`) generated
  from the seed data, and an audit doc (`PROMPT_FLOW_AUDIT.md`) mapping
  every prompt → which code path uses it → what placeholders it needs →
  what it outputs. When prompts and code drift (e.g. a prompt now expects
  a `{why_this_matters}` placeholder), that audit doc is what gets updated
  alongside the code change.

**Carry over:** put prompts in a datastore with a status/version field and
load them fresh per-run, not at import time. Validate the full required
set eagerly (fail the job, don't silently proceed with a stale/missing
prompt). Keep one living document that maps prompt-name → consumer
code → required variables → produced schema, and update it in the same
commit as any prompt/schema change.

---

## 4. LLM integration patterns

- **Structured output over free text wherever the result feeds downstream
  logic.** Every step that needs a schema (validation scores,
  categorization, country extraction, search-query generation, SEO
  metadata, translations, social captions, the enrichment paragraph) binds
  the model to a Pydantic/JSON schema and gets a typed object back, rather
  than parsing free text. Plain-text output is used only for the one case
  where the result *is* prose with nothing to extract (the summary itself).
- **Async everywhere.** All LLM calls are awaited; steps that don't need an
  LLM (link filtering, best-attempt selection, reading-time math) stay
  synchronous — don't make something async just for consistency if it does
  no I/O.
- **One model getter, one place.** A single `settings.get_model()` builds
  the chat client with model name/temperature from config; no step
  constructs its own client. Swapping providers/models is a one-line change.
- **No bespoke retry/backoff around individual LLM calls.** Reliability
  instead comes from (a) the generate/validate/retry loop described in
  §2.5 for the one step where output *quality* matters most, and (b) the
  fail-fast/swallow-and-continue split described in §2.4 for everything
  else. This is a deliberate simplicity choice — consider whether your new
  project's failure modes actually need per-call retry, or whether the
  same "isolate blast radius per step" approach is enough.
- **Tracing/observability is wired at the graph level, not per-call:** the
  compiled graph is registered once with a tracer (this project uses
  Opik), and the tracer is passed as a callback on each `ainvoke()` — so
  every step, prompt, and token count for a run is visible as one trace
  without any per-node instrumentation code.

---

## 5. The domain-specific enrichment step, as a template for "add a new step"

The most recently added step (`content_enrichment` — a "why this matters"
paragraph that contextualizes the article using previously-gathered search
results) is a good template for **how to add a new enrichment step to a
pipeline like this**:

1. **Define its output schema first** (`WhyThisMattersLLMOutput`: a single
   constrained field with a tight description — "one 80–120 word neutral
   paragraph" — the schema description *is* part of the prompt contract).
2. **Decide what upstream state it needs** and take only that (title,
   summary, categories, countries, a truncated content excerpt, and the
   search results already gathered by an earlier step) — don't re-fetch or
   re-derive anything another step already produced.
3. **Place it correctly in sequence** — after the steps that produce its
   inputs (country/category extraction, search results), before the steps
   that consume its output (translation needs the finished paragraph to
   translate; SEO/social steps run around it independently).
4. **Wire its output into every downstream consumer that should see it** —
   here, translation was updated in the same change to also translate
   `why_this_matters.content` into the localized variant, since a
   half-translated article would be a regression.
5. **Add both prompt fields (`_system`, `_user`) to the DB-backed prompt
   set and the required-prompts schema**, so the fail-fast check at
   `load_agent_configuration` covers it like every other step.
6. **Update the mapping doc** (prompt → consumer → variables → output)
   in the same change.

---

## 6. Runtime topology (for the new project's ops setup)

Six-ish services, cleanly separated by responsibility — reproduce the
*separation*, not necessarily the exact same technologies:

| Service | Responsibility | Swappable for |
|---|---|---|
| Queue + fast KV (Redis here) | job queue, dead-letter queue, job-status hash, distributed rate-limit locks | SQS/BullMQ/RabbitMQ + any KV |
| Primary datastore (MongoDB here) | prompts, domain config, final results, vector search | Postgres/any document or relational store + a vector extension |
| Headless browser service (Browserless here) | isolate browser automation as its own scalable service reached over CDP, not embedded per-worker | Playwright's own server mode, a managed scraping API |
| Self-hosted metasearch (SearXNG here) | grounding/context search without depending on a paid search API directly | Any search API, or SearXNG itself |
| API process | accept work, admin CRUD, health/debug endpoints | any web framework |
| Worker process | own the compiled graph, pop-and-run loop | any language/queue-consumer |
| Scheduler process | cron-style sourcing of new work + receiving the final webhook | any cron/interval scheduler |

**Two triggering paths feed the same queue** — a scheduled/automatic path
(periodic scan for new work) and a manual/API path (direct submission) —
both simply enqueue; the worker doesn't know or care which path a job came
from. Keep that boundary clean in the new project too: anything that can
*create* a job should only ever be able to enqueue, never invoke the
pipeline in-process.

---

## 7. Config & secrets pattern

- One settings object (this project: `pydantic-settings` `BaseSettings`),
  loaded once from environment variables with sane local-dev defaults,
  exposed as a module-level singleton. Every other module imports the
  singleton rather than reading `os.environ` directly.
- Every inter-service HTTP call (scheduler → API, webhook → scheduler) is
  gated by a shared-secret header, not a full auth system — appropriate
  for internal service-to-service calls behind a private network. External
  API keys (OpenAI, S3, SMTP) come from env vars with no defaults, so
  missing config fails at first use rather than silently degrading.
- A checked-in `sample.env` documents every variable (name + default/blank)
  without committing real values; the real `.env` is gitignored.

---

## 8. Visualizing the graph (optional but cheap to add)

If the new orchestrator exposes a graph-introspection API, wire a debug
endpoint that renders it (this project: `GET /debug/draw-graph`, backed by
`graph.get_graph(xray=True).draw_mermaid_png(...)`, output written to a
timestamped file under `graphs/`). Useful for onboarding and for catching
accidental topology changes in review — cheap enough to be worth
replicating even in a from-scratch stack (most orchestration libraries and
even a hand-rolled DAG can be dumped to Mermaid/DOT with a few lines).

---

## 9. Summary checklist for porting this to a new project

- [ ] One shared, versionable state schema; one nested "payload" object for
      the actual output being built up.
- [ ] Steps are pure-ish: `(state) -> state`, always return copies, never
      mutate.
- [ ] Classify each step critical-path vs. best-effort; pick
      halt-on-error vs. swallow-and-continue accordingly.
- [ ] The one place quality matters most gets a generate → critique
      (structured score) → conditional retry-with-feedback → pick-best-
      of-all-attempts loop, bounded by a max-retry cap, routing logic
      centralized in one function.
- [ ] Prompts live in a datastore with status/version, loaded fresh per
      run, validated eagerly (fail fast on missing required prompt) —
      not hardcoded strings.
- [ ] Structured/schema-validated LLM output everywhere the result drives
      downstream logic; free text only where the result is the final prose.
- [ ] One function builds the LLM client; every step reuses it.
- [ ] Split into an API (accept + enqueue only), a worker (own the graph,
      consume the queue), and — if there's a "find new work" concern — a
      separate scheduler; none of the three should do another's job.
- [ ] Durability via queue + status hash + terminal webhook/DB write, not
      orchestrator-native checkpointing, unless mid-run resume is a real
      requirement.
- [ ] One settings singleton from env vars; shared-secret headers for
      internal service-to-service calls.
- [ ] A living prompt/step mapping doc, updated in the same commit as any
      step or prompt change.

---

## Appendix: NewsAgent's concrete node sequence (for reference)

```
load_agent_configuration   (load prompts + category map from DB)
  → fetch_content            (scrape: headless browser + 3-tier extraction fallback)
  → extract_links            (parse + filter embedded links)
  → generate_summary ⟲       (LLM, plain text)
      ↳ validate_summary     (LLM, structured score) → [regenerate | end_loop]
  → select_best_summary      (pure function, pick max score from all attempts)
  → check_embedded_links     (parallel scrape + LLM relevance scoring)
  → find_other_sources       (LLM search-query gen → external search → dedup)
  → categorize_article       (LLM structured categorization + fuzzy ID match)
  → extract_country          (LLM structured extraction)
  → content_enrichment       (LLM "why this matters" — uses other_sources as grounding)
  → generate_seo              (LLM structured SEO fields + code-built JSON-LD)
  → translate_article         (LLM structured translation, incl. enrichment content)
  → calculate_reading_time    (pure function, word-count math)
  → generate_social_media     (LLM structured multi-platform captions)
  → notify_webhook            (POST final payload to external service)
  → END
```

Source: `src/graph/graph.py`, `src/graph/nodes/*.py`, `src/models/*.py`,
`PROMPT_FLOW_AUDIT.md`.
