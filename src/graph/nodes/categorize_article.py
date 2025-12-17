import traceback
from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState
from src.models.CategorizationModel import CategorizationModel
from src.configs.settings import settings
from langchain_core.prompts import PromptTemplate

# Import the prompts for this node
# from src.prompts.CategorizationPrompts import SYSTEM_PROMPT, USER_PROMPT

def categorize_article(state: MainWorkflowState) -> MainWorkflowState:
    """
    Assigns a list of categories (max 3) and a list of
    sub-categories (no limit) to the article.
    """
    pprint("[NODE 8: CATEGORIZE ARTICLE] Starting categorization...")

    try:
        # 1. Guards: Check if we have content
        if not state.news_article or not state.news_article.summary:
            pprint("[NODE 8: CATEGORIZE ARTICLE] No article/summary. Skipping.")
            return state.model_copy(update={
                "error_message": "No article/summary found for categorization."
            })

        prompts = state.active_prompts

        title = state.news_article.title
        summary = state.news_article.summary
        content_snippet = state.cleaned_article_text[:500]

        # 2. Get LLM with structured output
        model = settings.get_model()
        structured_llm = model.with_structured_output(CategorizationModel)

        # 3. Format the prompt
        prompt = PromptTemplate.from_template(prompts.categorization_user)
        formatted_prompt = prompt.format(
            title=title,
            summary=summary,
            content_snippet=content_snippet
        )

        messages = [
            ("system", prompts.categorization_system),
            ("user", formatted_prompt)
        ]

        pprint("[NODE 8: CATEGORIZE ARTICLE] Invoking classifier LLM...")

        # 4. Call the LLM
        response: CategorizationModel = structured_llm.invoke(messages)

        pprint(f"[NODE 8: CATEGORIZE ARTICLE] Categories assigned: {response.categories}")
        pprint(f"[NODE 8: CATEGORIZE ARTICLE] Sub-categories assigned: {response.sub_categories}")

        # 5. Update the ArticleModel in the state
        # --- UPDATED MAPPING ---
        updated_article = state.news_article.model_copy(update={
            "category": response.categories,
            "sub_category": response.sub_categories
        })
        # --- END UPDATES ---

        return state.model_copy(update={
            "news_article": updated_article
        })

    except Exception as e:
        pprint(f"[NODE 8: CATEGORIZE ARTICLE] Error during categorization: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in categorize_article: {e}"
        })