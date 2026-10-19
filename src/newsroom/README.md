# Newsroom code (NewsAgent)

The Newsroom is the backoffice view where reviewers see articles and signals together, link them to companies and places (the entity graph), find related items, and compose new Propt Newsroom articles and signals from them. This note explains how its code is organised and why it looks different from the rest of NewsAgent in a few places.

## On and off

Everything here is behind `NEWSROOM_ENABLED` (set it on the scheduler **and** the worker). Unset or `false`: no Newsroom route is mounted, no index is created, no job is scheduled, and the article pipeline behaves exactly as before. Settings are in `src/configs/settings.py` (`NEWSROOM_*`, `REAL_ESTATE_CHECK_ENABLED`) and documented in the main README and `sample.env`.

## Why it is a separate package

Most NewsAgent routes are written directly in `src/scheduler/main.py`. The Newsroom instead lives in its own package with its own FastAPI router (`router.py`, mounted at `/newsroom` by `mount_newsroom` when the switch is on). This is deliberate:

- it can be switched on and off as one piece;
- it stays apart from the existing code, so the existing behaviour is easy to prove unchanged;
- the `develop` → `production` merge stays conflict-free. `production` has worker shutdown (Fargate Spot SIGTERM) code in `src/worker.py` and `src/scheduler/main.py` that `develop` lacks, so the Newsroom keeps its changes to those two files small. Check `git merge-tree --write-tree origin/production HEAD` after touching them.

## Package map

| File | What it does |
|---|---|
| `router.py` | All `/newsroom` routes (existing `X-API-Key` check; `X-Newsroom-User` is recorded as the actor) |
| `models/` | API request and response models, split by area: `common` (shared types and size limits), `status`, `entities`, `links`, `related`, `compose`. Everything is re-exported from `src.newsroom.models` |
| `db.py` | Collection names, indexes (created at scheduler startup), and `ItemSources` (where articles and signals are read from) |
| `normalize.py`, `resolve.py` | Name normalising and the shared matching rules (exact aliases only; ambiguous names, ignored strings and websites/handles never match) |
| `seed.py`, `seed/` | The starting lists of companies and places (CSV) and how they are loaded |
| `entities.py`, `entity_csv.py` | Entity manager: create, edit, names, merge, delete, ignore list, CSV export/import |
| `links.py`, `locations.py` | Suggestions, reviewer links and unlinks, the New Names queue; places on links carry their parents (Dubai Creek Harbour → Dubai → UAE) |
| `related.py` | Related items, an entity's history, the Newsroom list filter by company or place |
| `pipeline.py` | Hooks the scheduler calls when an article is stored, edited, moved or changes status |
| `resync.py` | The nightly repair of link details that missed an update |
| `compose.py` | Compose requests: checks (source mix, type, desk), limits and the daily budget, the run record, saving signal drafts |
| `compose_worker.py`, `compose_graph.py` | The compose job in the worker: researcher → writer → editor, then the normal article steps (reused graph nodes) for composed articles |
| `prompts.py`, `desks.py`, `signal_options.py` | Compose prompts (news structure, newsroom rules, types), the nine specialist desks, Grok Scout's signal lists |
| `public_links.py` | The public propt.global link of a composed article (its stored url is internal: `newsroom://compose/<run>`) |
| `status.py` | Health checks for the backoffice banner and the `NEWSROOM_ALERT` log lines for CloudWatch |
| `backfill.py`, `backfill_trial.py` | One-off linking of older approved articles, and its read-only trial |

## Where it touches existing NewsAgent code

Small, switch-guarded additions; nothing existing is removed:

- `src/scheduler/main.py`: mounts the router; creates indexes; schedules the nightly repair and the alert check; calls the `pipeline.py` hooks from the result webhook, edits, archive/delete and status changes; stores composed articles' Newsroom fields.
- `src/worker.py`: a separate compose listener (`compose_loop`) with the worker heartbeat; the "not real estate" branch.
- `src/graph/nodes/extract_country.py`, `notify_webhook.py`, `src/models/MainWorkflowState.py`: the country step also returns company/project/place names (`newsroom_names`), sent next to the article, never inside it.
- `src/graph/nodes/check_real_estate.py` (off unless `REAL_ESTATE_CHECK_ENABLED`): skips articles that are not about real estate.
- `src/utils/loader.py`: composed articles get their public link in chatbot citations.
- `src/scripts/init_db.py`: writes the Newsroom prompts like every other prompt.

## Conventions used here

- **Prompts:** the code is the source of truth. `init_db.py --mode upsert` writes them to the prompt registry; in production run it after the new code is live.
- **Logging:** the `newsroom` logger (Python `logging`), not `print`, so CloudWatch can filter by level. Problems that need attention are logged as `NEWSROOM_ALERT <code>: <message>`.
- **Where the data lives:** the Newsroom's own collections (`entities`, `entity_aliases`, `entity_mentions`, `entity_suggestions`, `entity_audit`, `compose_runs`, ...) are in NewsAgent's database (`MONGO_DB_NAME`), next to the articles; composed articles are ordinary documents in `processed_articles`. Signals stay in social_scrapper's database (`NEWSROOM_SIGNALS_DB_NAME`, collection `signals`); composed signals are ordinary signals there.
- **Data ownership:** NewsAgent is the only writer of the Newsroom collections. Signals belong to social_scrapper; NewsAgent only reads them.
- **Scripts:** `src/scripts/newsroom_*.py` do a dry run by default and need `--apply` to write (backfill also has `--undo`).
- **Comments:** heavier than elsewhere on purpose, because many rules here protect precision (only reviewer-confirmed links), money (compose limits and budget) and published content.

## Tests

`tests/newsroom/` plus `tests/test_worker_newsroom.py`, `tests/test_real_estate_check.py` and `tests/test_country_check.py` (pytest, Python 3.11). Database tests need `TEST_MONGO_URL` pointing at a throwaway MongoDB; each test uses and drops its own `newsroom_test_*` database. AI calls are always replaced by fakes; tests never call OpenAI.

## The other repositories

- **social_scrapper:** `src/newsroom_client.py` tells NewsAgent about new and changed signals (`KG_API_URL`, `KG_API_KEY`); signal endpoints accept the Newsroom fields (`generated_from`, `article_id`, topics, sent-notification records).
- **propt-backoffice:** the Newsroom pages are in `src/app/(DashboardLayout)/newsroom/`, behind `NEXT_PUBLIC_NEWSROOM_ENABLED`; all calls go through `newsroomApi` in `src/services/api.ts`.
