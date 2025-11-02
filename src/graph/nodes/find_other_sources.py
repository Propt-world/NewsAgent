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

    Saves both the search results and the generated queries to the state.
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

        # --- STAGE 1: GENERATE QUERIES ---
        pprint("[NODE: FIND OTHER SOURCES] Generating search queries...")

        # Get LLM with structured output for our Query model
        query_gen_model = settings.get_model().with_structured_output(SearchQueryModel)

        # Format the prompt
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

        # Call the LLM to get our list of queries
        query_response: SearchQueryModel = query_gen_model.invoke(messages)
        search_queries = query_response.queries

        pprint(f"[NODE: FIND OTHER SOURCES] Generated {len(search_queries)} queries.")
        if query_response.keywords:
            pprint(f"[NODE: FIND OTHER SOURCES] Keywords: {query_response.keywords}")

        # --- STAGE 2: EXECUTE SEARCHES ---

        # Get the Tavily tool, configured for 3 results per query
        # This will use your corrected get_tavily_tool method
        tavily_tool = settings.get_tavily_tool(max_results=3)

        all_results: List[Dict[str, Any]] = []
        seen_urls: Set[str] = {state.source_url} # De-duplicate, ignore original

        for query in search_queries:
            pprint(f"[NODE: FIND OTHER SOURCES] Executing query: {query}")
            try:
                # Use .invoke() which now correctly uses TavilySearch
                results = tavily_tool.invoke(query)

                for res in results:
                    if res.get('url') not in seen_urls:
                        all_results.append(res)
                        seen_urls.add(res.get('url'))
            except Exception as e:
                pprint(f"[NODE: FIND OTHER SOURCES] Error on query '{query}': {e}")

        pprint(f"[NODE: FIND OTHER SOURCES] Found {len(all_results)} total unique results.")

        # 5. Update the state
        return state.model_copy(update={
            "other_sources": all_results,
            "search_query_data": query_response  # <-- Save the queries as requested
        })

    except Exception as e:
        pprint(f"[NODE: FIND OTHER SOURCES] Error during web search: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in find_other_sources: {e}"
        })