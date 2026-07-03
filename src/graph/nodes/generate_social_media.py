import traceback
from pprint import pprint
from langchain_core.prompts import PromptTemplate
from src.models.MainWorkflowState import MainWorkflowState
from src.models.SocialCaptionModel import SocialCaptionModel
from src.configs.settings import settings

async def generate_social_media(state: MainWorkflowState) -> MainWorkflowState:
    """
    Node: GENERATE SOCIAL MEDIA CAPTION
    Role: Master Copywriter (Conversion Focused)
    """
    pprint("[NODE: SOCIAL MEDIA] Generating high-conversion caption...")

    # --- 0. FAIL FAST CHECK ---
    if state.error_message:
        return state

    try:
        # 1. Guards
        if not state.news_article or not state.news_article.summary:
            pprint("[NODE: SOCIAL MEDIA] No summary available. Skipping.")
            return state

        # 2. Get Resources
        prompts = state.active_prompts
        model = settings.get_model().with_structured_output(SocialCaptionModel)

        # 3. Format Prompt
        # We pass the title, summary, and reading time to give the copywriter context
        reading_time = state.news_article.reading_time or "2"
        
        prompt = PromptTemplate.from_template(prompts.social_caption_user)
        formatted_prompt = prompt.format(
            title=state.news_article.title,
            summary=state.news_article.summary,
            reading_time=reading_time
        )

        messages = [
            ("system", prompts.social_caption_system),
            ("user", formatted_prompt)
        ]

        # 4. Invoke LLM
        pprint("[NODE: SOCIAL MEDIA] Invoking Copywriter LLM...")
        caption_result: SocialCaptionModel = await model.ainvoke(messages)

        pprint(f"[NODE: SOCIAL MEDIA] Generated Hook: {caption_result.headline}")

        # 5. Update State
        updated_article = state.news_article.model_copy(update={
            "social_media": caption_result
        })

        return state.model_copy(update={"news_article": updated_article})

    except Exception as e:
        pprint(f"[NODE: SOCIAL MEDIA] Error: {e}")
        traceback.print_exc()
        # Non-critical failure, return state as is
        return state
