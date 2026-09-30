# Updated Prompt Flow Audit

Source prompt file checked: `UPDATED_PROMPTS_FOR_DB.md`

LangGraph flow checked:

```text
load_agent_configuration -> raw_extraction -> extract_links -> generate_summary / validate_summary retry loop -> select_best_summary -> check_embedded_links -> find_other_sources -> categorize_article -> extract_country -> content_enrichment -> generate_seo -> translate_article -> calculate_reading_time -> generate_social_media -> notify_webhook
```

## Result

All 21 updated prompt templates now have their required placeholders mapped by the graph or helper code.

The audit found and fixed these mismatches:

| Area | Updated prompt requirement | Fix applied |
| --- | --- | --- |
| SEO | `seo_system` asks for `h1` and a singular `primary_keyword`. | Added `h1` and `primary_keyword` to `SeoLLMOutput` / `SeoMetadataModel`. |
| Summary validation | `validation_system` asks the critic to run a TTS/format check. | Added optional `format_check_passed` to `ValidationResultModel`. |
| Translation | `translation_user` requires `{why_this_matters}`. | `translate_article` now passes `why_this_matters` and stores `why_this_matters.content_ar`. |
| Social captions | `social_caption_user` requires `{platforms}` and `{cta_target}`. | `generate_social_media` and `backfill_social` now pass configurable defaults. |
| Social caption output | Prompt asks for one caption per requested platform. | Added `platform_captions` variants while preserving the existing top-level social fields. |
| Content enrichment | Prompt now asks for one sentence, not a paragraph. | Updated model descriptions to match. |
| Categorization | Prompt asks for 3-4 categories. | Updated article category description to match. |

## Prompt-To-Code Mapping

| Prompt | Code path | Placeholder mapping | Output target |
| --- | --- | --- | --- |
| `summary_system` / `summary_initial_user` | `src/graph/nodes/summary_generator.py` | `article_text` | `news_article.summary` |
| `summary_retry_user` | `src/graph/nodes/summary_generator.py` | `feedback`, `article_text` | `news_article.summary` |
| `validation_system` / `validation_user` | `src/graph/nodes/validate_summary.py` | `article_text`, `summary_text` | `validation_result`, `summary_attempts` |
| `relevance_system` / `relevance_user` | `src/graph/nodes/check_embedded_links.py` | `summary`, `link_context`, `link_content` | `embedded_links[].relevance_score` |
| `search_system` / `search_user` | `src/graph/nodes/find_other_sources.py` | `title`, `summary`, `publish_date` | `search_query_data`, `other_sources` |
| `categorization_system` / `categorization_user` | `src/graph/nodes/categorize_article.py` | `title`, `summary`, `content_snippet` | `category`, `category_ids` |
| `seo_system` / `seo_user` | `src/graph/nodes/generate_seo.py` | `title`, `summary`, `content_snippet` | `seo`, rewritten `title`, `source_title` |
| `country_extraction_system` / `country_extraction_user` | `src/graph/nodes/extract_country.py` | `title`, `summary`, `content` | `countries` |
| `content_enrichment_system` / `content_enrichment_user` | `src/graph/nodes/content_enrichment.py` | `title`, `summary`, `categories`, `countries`, `article_excerpt`, `contextual_sources` | `why_this_matters.content`, `why_this_matters.references` |
| `translation_system` / `translation_user` | `src/graph/nodes/translate_article.py` | `title`, `summary`, `why_this_matters`, `content` | `title_ar`, `summary_ar`, `content_ar`, `why_this_matters.content_ar` |
| `social_caption_system` / `social_caption_user` | `src/graph/nodes/generate_social_media.py`; `src/utils/backfill_social.py` | `title`, `summary`, `reading_time`, `platforms`, `cta_target` | `social_media`, `social_media.platform_captions` |

## Verification

- Static placeholder audit passed for all 21 updated prompts.
- Python compile check passed for touched model, graph-node, settings, and social-backfill files.
- `src/scripts/init_db.py` prompt seed content and `input_variables` now match `UPDATED_PROMPTS_FOR_DB.md`.

## DB Initialization

`src/scripts/init_db.py --mode upsert` will now upsert the updated prompt text into active prompt records. The seed script still preserves its existing behavior: it updates active prompts by `name` + `status`, and it leaves article/archive/vector data alone unless `--mode fresh --yes` is used.
