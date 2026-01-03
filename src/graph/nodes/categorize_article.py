import traceback
import re
from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState
from src.models.CategorizationModel import CategorizationModel
from src.configs.settings import settings
from langchain_core.prompts import PromptTemplate

# Import the prompts for this node
# from src.prompts.CategorizationPrompts import SYSTEM_PROMPT, USER_PROMPT

def categorize_article(state: MainWorkflowState) -> MainWorkflowState:
    """
    Assigns a list of categories (max 3) to the article.
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

        predicted_categories = response.categories
        pprint(f"[NODE 8: CATEGORIZE ARTICLE] LLM Raw Output: {predicted_categories}")

        # 4. Resolve IDs with Normalization (The Fix)
        mapped_ids = []
        category_map = state.category_mapping or {}
        
        # Create a normalized lookup map (lowercase, no punctuation)
        # e.g. "architecture  design trends" -> "uuid-123"
        normalized_map = {}
        for name, uid in category_map.items():
            clean_key = re.sub(r'[^\w\s]', '', name).lower().strip()
            normalized_map[clean_key] = uid

        for cat_name in predicted_categories:
            # 1. Try Exact Match first
            if cat_name in category_map:
                mapped_ids.append(category_map[cat_name])
                continue
            
            # 2. Try Normalized Match (Handles **Bolding** or whitespace)
            clean_name = re.sub(r'[^\w\s]', '', cat_name).lower().strip()
            if clean_name in normalized_map:
                found_id = normalized_map[clean_name]
                mapped_ids.append(found_id)
                pprint(f"[NODE 8] Fuzzy matched '{cat_name}' to ID {found_id}")
            else:
                pprint(f"[WARNING] Could not map category '{cat_name}' (Cleaned: '{clean_name}')")

        pprint(f"[NODE 8] Final Mapped IDs: {mapped_ids}")

        # 5. Update State
        updated_article = state.news_article.model_copy(update={
            "category": predicted_categories,
            "category_ids": mapped_ids
        })

        return state.model_copy(update={"news_article": updated_article})

    except Exception as e:
        pprint(f"[NODE 8: CATEGORIZE ARTICLE] Error: {e}")
        traceback.print_exc()
        return state.model_copy(update={"error_message": f"Error in categorize: {e}"})