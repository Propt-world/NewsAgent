import traceback
from pprint import pprint
from langchain_core.prompts import PromptTemplate
from src.models.MainWorkflowState import MainWorkflowState
from src.models.TranslationModel import TranslationModel
from src.configs.settings import settings

def translate_article(state: MainWorkflowState) -> MainWorkflowState:
    """
    Translates the title, summary, and content of the article into Arabic.
    """
    pprint("[NODE: TRANSLATE] Starting Arabic translation...")

    # --- 0. FAIL FAST CHECK ---
    if state.error_message:
        return state

    try:
        # 1. Guard: Check if article exists
        if not state.news_article or not state.news_article.content:
            pprint("[NODE: TRANSLATE] No content to translate. Skipping.")
            return state

        # 2. Get Prompts & Model
        prompts = state.active_prompts
        # Use structured output to ensure we get title/summary/content separated cleanly
        model = settings.get_model().with_structured_output(TranslationModel)

        # 3. Format Prompt
        user_prompt_template = PromptTemplate.from_template(prompts.translation_user)
        formatted_prompt = user_prompt_template.format(
            title=state.news_article.title,
            summary=state.news_article.summary or "",
            content=state.news_article.content
        )

        messages = [
            ("system", prompts.translation_system),
            ("user", formatted_prompt)
        ]

        # 4. Invoke LLM
        # Note: If content is very long, this might take a moment.
        pprint("[NODE: TRANSLATE] Invoking LLM for translation...")
        translation_result: TranslationModel = model.invoke(messages)

        pprint(f"[NODE: TRANSLATE] Translation complete. Title: {translation_result.title_ar}")

        # 5. Update Article Model
        updated_article = state.news_article.model_copy(update={
            "title_ar": translation_result.title_ar,
            "summary_ar": translation_result.summary_ar,
            "content_ar": translation_result.content_ar
        })

        return state.model_copy(update={
            "news_article": updated_article
        })

    except Exception as e:
        pprint(f"[NODE: TRANSLATE] Error during translation: {e}")
        traceback.print_exc()
        # We generally don't want to fail the whole workflow just because translation failed,
        # so we return the state as-is (or with an error flag if you prefer).
        return state