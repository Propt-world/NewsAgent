import traceback
from pprint import pprint
from typing import Dict, List

from langchain_core.prompts import PromptTemplate

from src.configs.settings import settings
from src.models.MainWorkflowState import MainWorkflowState
from src.models.WhyThisMattersModel import (
    ContextualReferenceModel,
    WhyThisMattersLLMOutput,
    WhyThisMattersModel,
)


async def enrich_content(state: MainWorkflowState) -> MainWorkflowState:
    """
    Adds a neutral "Why this matters" contextual section to the article.
    """
    pprint("[NODE: CONTENT ENRICHMENT] Generating Why this matters section...")

    if state.error_message:
        return state

    try:
        if not state.news_article or not state.news_article.summary:
            pprint("[NODE: CONTENT ENRICHMENT] No article/summary. Skipping.")
            return state.model_copy(update={
                "error_message": "No article/summary found for content enrichment."
            })

        prompts = state.active_prompts
        if not prompts.content_enrichment_system or not prompts.content_enrichment_user:
            pprint(
                "[NODE: CONTENT ENRICHMENT] Prompts are not configured. Skipping."
            )
            return state

        context_results = state.other_sources[:settings.SEARCH_MAX_RESULTS]
        references = _build_references(context_results)
        context_block = _format_context_results(context_results)

        model = settings.get_model().with_structured_output(WhyThisMattersLLMOutput)
        prompt = PromptTemplate.from_template(prompts.content_enrichment_user)
        formatted_prompt = prompt.format(
            title=state.news_article.title,
            summary=state.news_article.summary,
            categories=", ".join(state.news_article.category) or "Not available",
            countries=", ".join(state.news_article.countries) or "Not available",
            article_excerpt=(state.cleaned_article_text or "")[:1500],
            contextual_sources=context_block,
        )

        messages = [
            ("system", prompts.content_enrichment_system),
            ("user", formatted_prompt),
        ]

        response: WhyThisMattersLLMOutput = await model.ainvoke(messages)
        why_this_matters = WhyThisMattersModel(
            content=response.content.strip(),
            references=references,
        )

        updated_article = state.news_article.model_copy(update={
            "why_this_matters": why_this_matters
        })

        pprint("[NODE: CONTENT ENRICHMENT] Why this matters generated successfully.")
        return state.model_copy(update={"news_article": updated_article})

    except Exception as e:
        pprint(f"[NODE: CONTENT ENRICHMENT] Error: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Content enrichment failed: {e}"
        })


def _build_references(results: List[Dict]) -> List[ContextualReferenceModel]:
    references = []
    for result in results:
        url = result.get("url")
        if not url:
            continue

        references.append(
            ContextualReferenceModel(
                title=result.get("title"),
                url=url,
                source=result.get("source"),
                snippet=(result.get("content") or "")[:300],
            )
        )

    return references


def _format_context_results(results: List[Dict]) -> str:
    if not results:
        return (
            "No external contextual sources were available. Rely only on the "
            "article facts and avoid broad market claims."
        )

    lines = []
    for index, result in enumerate(results, start=1):
        title = result.get("title") or "Untitled"
        source = result.get("source") or "unknown source"
        url = result.get("url") or ""
        content = (result.get("content") or "")[:600]
        lines.append(
            f"{index}. Title: {title}\n"
            f"Source: {source}\n"
            f"URL: {url}\n"
            f"Snippet: {content}"
        )

    return "\n\n".join(lines)
