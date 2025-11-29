import traceback
from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState
from src.configs.settings import settings
from langchain_core.prompts import PromptTemplate
from src.prompts.SummaryPrompts import SYSTEM_PROMPT, INITIAL_USER_PROMPT, RETRY_USER_PROMPT

def generate_summary(state: MainWorkflowState) -> MainWorkflowState:
    """
    Generates a summary of the 'cleaned_article_text' using the
    LLM configured in settings.

    - If it's a retry (validation_result exists), it incorporates
      the feedback into a new prompt.
    - Note: validation_count is incremented in validate_summary node.
    """
    pprint(f"[NODE: SUMMARY GENERATOR] Starting summary generation (Attempt #{state.validation_count + 1})...")

    try:
        # 1. Guards: Check if we have the necessary content
        if not state.cleaned_article_text:
            pprint("[NODE: SUMMARY GENERATOR] Error: cleaned_article_text is missing.")
            return state.model_copy(update={
                "error_message": "Cannot generate summary: cleaned_article_text is missing."
            })
        if not state.news_article:
            pprint("[NODE: SUMMARY GENERATOR] Error: news_article model is missing.")
            return state.model_copy(update={
                "error_message": "Cannot generate summary: news_article model is missing."
            })

        # 2. Get the LLM from your project's settings
        # We don't use .with_structured_output() here because
        # we just want a string summary, not a Pydantic model.
        model = settings.get_model()

        # 3. Choose the correct prompt template (initial vs. retry)
        article_text = state.cleaned_article_text

        if state.validation_result and state.validation_result.feedback != "Validation not yet run.":
            pprint("[NODE: SUMMARY GENERATOR] This is a retry. Incorporating feedback.")
            feedback = state.validation_result.feedback
            template = RETRY_USER_PROMPT
            prompt = PromptTemplate.from_template(template)
            formatted_prompt = prompt.format(
                feedback=feedback,
                article_text=article_text
            )
        else:
            pprint("[NODE: SUMMARY GENERATOR] This is the first attempt.")
            template = INITIAL_USER_PROMPT
            prompt = PromptTemplate.from_template(template)
            formatted_prompt = prompt.format(article_text=article_text)

        # 4. Create the full message list and invoke the model
        messages = [
            ("system", SYSTEM_PROMPT),
            ("user", formatted_prompt)
        ]

        response = model.invoke(messages)
        summary_text = response.content # .content has the string output

        # 5. Update the state
        updated_article = state.news_article.model_copy(update={
            "summary": summary_text
        })

        pprint("[NODE: SUMMARY GENERATOR] Summary generated successfully.")

        return state.model_copy(update={
            "news_article": updated_article,
            "validation_result": None # Clear old feedback
        })

    except Exception as e:
        pprint(f"[NODE: SUMMARY GENERATOR] Error during summarization: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in generate_summary: {e}"
        })