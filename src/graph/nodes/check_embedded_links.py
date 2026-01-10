import asyncio
import traceback
from pprint import pprint
from typing import List
from bs4 import BeautifulSoup
from langchain_core.prompts import PromptTemplate

# Project Imports
from src.models.MainWorkflowState import MainWorkflowState
from src.models.EmbeddedLinkModel import EmbeddedLinkModel
from src.models.RelevanceScoreModel import RelevanceScoreModel
from src.configs.settings import settings

# Import the Semaphore and Launcher from our new browser manager
from src.utils.browser import get_async_browser_context, BROWSER_SEMAPHORE

async def _process_links_batch(
    links: List[EmbeddedLinkModel], # 1. We accept the FULL LIST to process in parallel
    summary: str,
    sys_prompt: str,
    user_prompt: str
) -> List[EmbeddedLinkModel]:
    """
    Orchestrates checking multiple links.
    KEY STRATEGY: One Browser Instance, Multiple Tabs (Contexts).
    """
    
    # --- A. RESOURCE SETUP ---
    # Launching a browser is expensive (CPU-wise). We do it ONCE for the whole batch.
    async with get_async_browser_context() as (p, browser):
    
        # --- B. INITIALIZE LLM INTERNALY ---
        # 2. The LLM is initialized HERE, so we don't need to pass it as an argument.
        # It captures the settings and is ready to be used by the worker function below.
        llm = settings.get_model().with_structured_output(RelevanceScoreModel)

        # --- Inner Worker Function (Closure) ---
        # Defined inside so it can access 'browser' and 'llm' without passing them as args
        async def check_single_link(link: EmbeddedLinkModel):
            
            # --- C. CONCURRENCY CONTROL ---
            # "Wait here until there are fewer than 8 active tabs"
            async with BROWSER_SEMAPHORE: 
                context = None
                try:
                    # Create a lightweight "Context" (like a new Incognito window)
                    context = await browser.new_context(user_agent=settings.USER_AGENT)
                    page = await context.new_page()
                    
                    # Fast timeout (15s) - we don't need perfect rendering for relevance check
                    await page.goto(link.url, timeout=15000, wait_until="domcontentloaded")
                    
                    # Extract Text
                    html = await page.content()
                    soup = BeautifulSoup(html, "lxml")
                    text = soup.get_text(separator=" ", strip=True)[:1500]

                    # --- D. USE THE LLM ---
                    # We use the prompts passed in arguments and the LLM initialized above
                    prompt = PromptTemplate.from_template(user_prompt)
                    fmt_prompt = prompt.format(
                        summary=summary,
                        link_context=link.context,
                        link_content=text
                    )
                    
                    # Async LLM call
                    res = await llm.ainvoke([("system", sys_prompt), ("user", fmt_prompt)])
                    
                    return link.model_copy(update={"relevance_score": res.score})

                except Exception:
                    # If anything fails (timeout, 404), mark score as 0.0.
                    # Do NOT crash the whole batch for one bad link.
                    return link.model_copy(update={"relevance_score": 0.0})
                finally:
                    # Close tab immediately to free up the Semaphore slot
                    if context: await context.close()

        # --- E. EXECUTE PARALLEL BATCH ---
        # Create a task for every link in the list
        tasks = [check_single_link(link) for link in links]
        
        # asyncio.gather runs them all at once (respecting the Semaphore limit)
        results = await asyncio.gather(*tasks)
        return results

async def check_embedded_links(state: MainWorkflowState) -> MainWorkflowState:
    """
    Main node function.
    """
    pprint("[NODE: CHECK LINKS] Starting throttled link scoring...")

    # --- 0. FAIL FAST CHECK ---
    if state.error_message:
        return state

    try:
        # Guards
        if not state.news_article or not state.news_article.embedded_links:
            return state

        # Extract prompts from State
        prompts = state.active_prompts
        
        # Run the async batch processor
        updated_links = await _process_links_batch(
            state.news_article.embedded_links,
            state.news_article.summary,
            prompts.relevance_system,
            prompts.relevance_user
        )

        updated_article = state.news_article.model_copy(update={
            "embedded_links": updated_links
        })
        return state.model_copy(update={"news_article": updated_article})

    except Exception as e:
        pprint(f"[NODE: CHECK LINKS] Error: {e}")
        # Return state as-is on error to avoid breaking the workflow
        return state

        updated_article = state.news_article.model_copy(update={
            "embedded_links": updated_links
        })
        return state.model_copy(update={"news_article": updated_article})

    except Exception as e:
        pprint(f"[NODE: CHECK LINKS] Error: {e}")
        # Return state as-is on error to avoid breaking the workflow
        return state