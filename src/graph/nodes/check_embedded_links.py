import asyncio
import traceback
from pprint import pprint
from typing import List
from requests_html import AsyncHTMLSession # Still useful for async requests
from bs4 import BeautifulSoup
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import PromptTemplate

# Project imports
from src.models.MainWorkflowState import MainWorkflowState
from src.models.EmbeddedLinkModel import EmbeddedLinkModel
from src.models.RelevanceScoreModel import RelevanceScoreModel
from src.configs.settings import settings
from src.prompts.RelevancePrompts import SYSTEM_PROMPT, USER_PROMPT

# --- Helper Function to score one link (NOW MUCH FASTER) ---

async def _async_score_single_link(
    session: AsyncHTMLSession,
    link: EmbeddedLinkModel,
    summary: str,
    llm: BaseChatModel
) -> EmbeddedLinkModel:
    """
    Async helper to fetch (static HTML) and score a single URL.
    Handles errors gracefully.
    """
    pprint(f"[NODE: CHECK LINKS] Scoring link: {link.url}")

    try:
        # 1. Fetch the static HTML
        response = await session.get(
            link.url,
            timeout=15, # Shorter timeout
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                              " AppleWebKit/537.36 (KHTML, like Gecko)"
            }
        )
        response.raise_for_status()

        # 2. --- NO JS RENDERING ---
        # We skip the "await response.html.arender()"
        # This is the massive performance gain.

        # 3. Extract text using BeautifulSoup
        soup = BeautifulSoup(response.text, "lxml") # Use response.text
        linked_text = soup.get_text(separator=" ", strip=True)

        if not linked_text:
            pprint(f"[NODE: CHECK LINKS] No text found at: {link.url}")
            return link.model_copy(update={"relevance_score": 0.0})

        linked_text_snippet = linked_text[:1500]

        # 4. Format prompt and call LLM (No change here)
        prompt = PromptTemplate.from_template(USER_PROMPT)
        formatted_prompt = prompt.format(
            summary=summary,
            link_context=link.context,
            link_content=linked_text_snippet
        )

        messages = [
            ("system", SYSTEM_PROMPT),
            ("user", formatted_prompt)
        ]

        response_model: RelevanceScoreModel = await llm.ainvoke(messages)

        pprint(f"[NODE: CHECK LINKS] Score for {link.url}: {response_model.score}")

        # 5. Return the updated link model
        return link.model_copy(update={
            "relevance_score": response_model.score
        })

    except Exception as e:
        pprint(f"[NODE: CHECK LINKS] Error scoring {link.url}: {e}")
        return link.model_copy(update={"relevance_score": 0.0})

# --- Async Runner (No change needed) ---

async def _run_all_link_checks(
    links: List[EmbeddedLinkModel],
    summary: str
) -> List[EmbeddedLinkModel]:
    """
    Creates an async session and runs all link checks in parallel.
    """
    llm = settings.get_model().with_structured_output(RelevanceScoreModel)

    async with AsyncHTMLSession() as session:
        tasks = []
        for link in links:
            tasks.append(_async_score_single_link(session, link, summary, llm))

        updated_links = await asyncio.gather(*tasks)
        return updated_links

# --- Main Graph Node (Sync - No change needed) ---

def check_embedded_links(state: MainWorkflowState) -> MainWorkflowState:
    """
F    Scores all embedded links for relevance in parallel (fast, no-JS).
    """
    pprint("[NODE: CHECK LINKS] Starting parallel link scoring (fast mode)...")

    try:
        # ... (Guards are the same) ...
        if not state.news_article or not state.news_article.summary:
            return state.model_copy(update={
                "error_message": "No article/summary found for link checking."
            })
        links = state.news_article.embedded_links
        summary = state.news_article.summary
        if not links:
            return state

        # This runs the async function
        updated_links = asyncio.run(_run_all_link_checks(links, summary))

        updated_article = state.news_article.model_copy(update={
            "embedded_links": updated_links
        })

        pprint("[NODE: CHECK LINKS] All links scored successfully.")

        return state.model_copy(update={
            "news_article": updated_article
        })

    except Exception as e:
        pprint(f"[NODE: CHECK LINKS] A critical error occurred: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in check_embedded_links: {e}"
        })