import asyncio
import traceback
from pprint import pprint
from typing import List
from requests_html import AsyncHTMLSession
from bs4 import BeautifulSoup
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import PromptTemplate

# Project imports
from src.models.MainWorkflowState import MainWorkflowState
from src.models.EmbeddedLinkModel import EmbeddedLinkModel
from src.models.RelevanceScoreModel import RelevanceScoreModel
from src.configs.settings import settings
from src.utils.browser import get_async_html_session
# from src.prompts.RelevancePrompts import SYSTEM_PROMPT, USER_PROMPT

# --- Helper Function to score one link ---

async def _async_score_single_link(
    session: AsyncHTMLSession,
    link: EmbeddedLinkModel,
    summary: str,
    llm: BaseChatModel,
    sys_prompt: str,
    user_prompt: str
) -> EmbeddedLinkModel:
    """
    Async helper to fetch (static HTML) and score a single URL.
    Handles errors gracefully.
    """
    try:
        # 1. Fetch the static HTML
        # We allow a short timeout to keep the pipeline moving
        response = await session.get(
            link.url,
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                              " AppleWebKit/537.36 (KHTML, like Gecko)"
            }
        )


        # 2. Extract text using BeautifulSoup
        # We skip .arender() here for speed and stability
        soup = BeautifulSoup(response.text, "lxml")
        linked_text = soup.get_text(separator=" ", strip=True)

        if not linked_text:
            return link.model_copy(update={"relevance_score": 0.0})

        linked_text_snippet = linked_text[:1500]

        # 3. Format prompt and call LLM
        prompt = PromptTemplate.from_template(user_prompt)
        formatted_prompt = prompt.format(
            summary=summary,
            link_context=link.context,
            link_content=linked_text_snippet
        )

        messages = [
            ("system", sys_prompt),
            ("user", formatted_prompt)
        ]

        # Use ainvoke for non-blocking LLM calls
        response_model: RelevanceScoreModel = await llm.ainvoke(messages)

        return link.model_copy(update={
            "relevance_score": response_model.score
        })

    except Exception as e:
        # Log error if needed, but return 0.0 to keep the pipeline alive
        # pprint(f"[NODE: CHECK LINKS] Error scoring {link.url}: {e}")
        return link.model_copy(update={"relevance_score": 0.0})

# --- Async Runner ---

async def _run_all_link_checks(
    links: List[EmbeddedLinkModel],
    summary: str
) -> List[EmbeddedLinkModel]:
    """
    Creates an async session and runs all link checks in parallel.
    """
    llm = settings.get_model().with_structured_output(RelevanceScoreModel)

    # 1. Initialize session normally (NOT in context manager)
    # These flags help stability in Docker even when using the bundled browser
    session = get_async_html_session()

    try:
        tasks = []
        for link in links:
            tasks.append(_async_score_single_link(session, link, summary, llm))

        # Run all tasks concurrently
        updated_links = await asyncio.gather(*tasks)
        return updated_links

    finally:
        # 2. Explicitly close the session to prevent resource leaks
        # This fixes the "does not support context manager" TypeError
        await session.close()

# --- Main Graph Node ---

def check_embedded_links(state: MainWorkflowState) -> MainWorkflowState:
    """
    Scores all embedded links for relevance in parallel.
    """
    pprint("[NODE: CHECK LINKS] Starting parallel link scoring...")

    try:
        # 1. Guards
        if not state.news_article or not state.news_article.summary:
            return state.model_copy(update={
                "error_message": "No article/summary found for link checking."
            })

        links = state.news_article.embedded_links
        summary = state.news_article.summary

        if not links:
            pprint("[NODE: CHECK LINKS] No links to check.")
            return state

        # 2. Get Prompts from State
        prompts = state.active_prompts
        sys_prompt = prompts.relevance_system
        user_prompt = prompts.relevance_user

        # 3. Run the async function
        # asyncio.run() handles the event loop for us
        updated_links = asyncio.run(_run_all_link_checks(
            links,
            summary,
            sys_prompt,
            user_prompt
        ))

        updated_article = state.news_article.model_copy(update={
            "embedded_links": updated_links
        })

        pprint(f"[NODE: CHECK LINKS] Scored {len(updated_links)} links successfully.")

        return state.model_copy(update={
            "news_article": updated_article
        })

    except Exception as e:
        pprint(f"[NODE: CHECK LINKS] A critical error occurred: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in check_embedded_links: {e}"
        })