import traceback
from pprint import pprint
from typing import Dict, Any, Set, List
from src.models.MainWorkflowState import MainWorkflowState
from src.models.SearchQueryModel import SearchQueryModel
from src.configs.settings import settings
from langchain_core.prompts import PromptTemplate

# Import the prompts for this node
from src.prompts.SearchQueryPrompts import SYSTEM_PROMPT, USER_PROMPT

def find_other_sources(state: MainWorkflowState) -> MainWorkflowState:
    """
    Generates multiple, high-quality search queries and then
    executes them to find corroborating sources for the article.
    """
    pprint("[NODE: FIND OTHER SOURCES] Starting multi-query search...")

    try:
        # 1. Guards: Check for content
        if not state.news_article or not state.news_article.summary:
            pprint("[NODE: FIND OTHER SOURCES] No article/summary. Skipping.")
            return state.model_copy(update={
                "error_message": "No article/summary found for web search."
            })

        title = state.news_article.title
        summary = state.news_article.summary
        publish_date = state.news_article.published_date or "Not available"

        # --- STAGE 1: GENERATE QUERIES (Same as before) ---
        pprint("[NODE: FIND OTHER SOURCES] Generating search queries...")

        query_gen_model = settings.get_model().with_structured_output(SearchQueryModel)

        prompt = PromptTemplate.from_template(USER_PROMPT)
        formatted_prompt = prompt.format(
            title=title,
            summary=summary,
            publish_date=publish_date
        )

        messages = [
            ("system", SYSTEM_PROMPT),
            ("user", formatted_prompt)
        ]

        query_response: SearchQueryModel = query_gen_model.invoke(messages)
        search_queries = query_response.queries

        pprint(f"[NODE: FIND OTHER SOURCES] Generated {len(search_queries)} queries.")

        # --- STAGE 2: EXECUTE SEARCHES (Updated for TavilyClient) ---

        # 1. Get the Client
        tavily_client = settings.get_tavily_client()

        all_results: List[Dict[str, Any]] = []
        seen_urls: Set[str] = {state.source_url}

        for query in search_queries:
            pprint(f"[NODE: FIND OTHER SOURCES] Executing query: {query}")
            try:
                # 2. Use .search() directly
                # returns: {'query': '...', 'results': [{'url': '...', 'content': '...'}, ...]}
                response = tavily_client.search(
                    query=query,
                    search_depth="basic",
                    max_results=5
                )

                results = response.get("results", [])

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