import asyncio
import traceback
from pprint import pprint
from typing import Dict, Any, Set, List

from langchain_core.prompts import PromptTemplate

from src.configs.settings import settings
from src.models.MainWorkflowState import MainWorkflowState
from src.models.SearchQueryModel import SearchQueryModel


async def find_other_sources(state: MainWorkflowState) -> MainWorkflowState:
    """
    Generates multiple, high-quality search queries and then
    executes them through the configured search provider to find
    corroborating sources for the article.
    """
    pprint("[NODE: FIND OTHER SOURCES] Starting multi-query search...")

    # --- 0. FAIL FAST CHECK ---
    if state.error_message:
        return state

    try:
        # 1. Guards: Check for content
        if not state.news_article or not state.news_article.summary:
            pprint("[NODE: FIND OTHER SOURCES] No article/summary. Skipping.")
            return state.model_copy(update={
                "error_message": "No article/summary found for web search."
            })

        prompts = state.active_prompts

        title = state.news_article.title
        summary = state.news_article.summary
        publish_date = state.news_article.published_date or "Not available"

        # --- STAGE 1: GENERATE QUERIES ---
        pprint("[NODE: FIND OTHER SOURCES] Generating search queries...")

        query_gen_model = settings.get_model().with_structured_output(SearchQueryModel)

        prompt = PromptTemplate.from_template(prompts.search_user)
        formatted_prompt = prompt.format(
            title=title,
            summary=summary,
            publish_date=publish_date
        )

        messages = [
            ("system", prompts.search_system),
            ("user", formatted_prompt)
        ]

        query_response: SearchQueryModel = await query_gen_model.ainvoke(messages)
        search_queries = query_response.queries

        pprint(f"[NODE: FIND OTHER SOURCES] Generated {len(search_queries)} queries.")

        # --- STAGE 2: EXECUTE SEARCHES ---
        search_client = settings.get_search_client()

        all_results: List[Dict[str, Any]] = []
        seen_urls: Set[str] = {state.source_url}

        for query in search_queries:
            pprint(f"[NODE: FIND OTHER SOURCES] Executing query: {query}")
            try:
                if hasattr(search_client, "search_async"):
                    results = await search_client.search_async(query=query)
                else:
                    results = await asyncio.to_thread(search_client.search, query=query)
                for res in results:
                    url = res.get('url')
                    if url and url not in seen_urls:
                        all_results.append(res)
                        seen_urls.add(url)

            except Exception as e:
                pprint(f"[NODE: FIND OTHER SOURCES] Error on query '{query}': {e}")

        pprint(f"[NODE: FIND OTHER SOURCES] Found {len(all_results)} total unique results.")

        # 5. Update the state
        return state.model_copy(update={
            "other_sources": all_results,
            "search_query_data": query_response
        })

    except Exception as e:
        pprint(f"[NODE: FIND OTHER SOURCES] Error during web search: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in find_other_sources: {e}"
        })

    except Exception as e:
        pprint(f"[NODE: FIND OTHER SOURCES] Error during web search: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in find_other_sources: {e}"
        })
