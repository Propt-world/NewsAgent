import traceback
from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ValidationResultModel import ValidationResultModel
from src.models.SummaryAttemptModel import SummaryAttemptModel
from src.configs.settings import settings
from langchain_core.prompts import PromptTemplate
from src.utils.prompt_lengths import fill_lengths
#from src.prompts.ValidationPrompts import SYSTEM_PROMPT, USER_PROMPT


def count_words(text: str) -> int:
    return len((text or "").split())


def check_word_count(result: ValidationResultModel, state: MainWorkflowState) -> ValidationResultModel:
    """
    Fails a summary outside state.summary_min_words-summary_max_words, with the count in the feedback so the retry
    knows how much to add or cut. The minimum is not enforced when the article itself is shorter than it
    (the summary must never be padded with repeated or invented text).
    """
    words = count_words(state.news_article.summary)
    low, high = state.summary_min_words, state.summary_max_words
    if count_words(state.cleaned_article_text) < low:
        low = 0
    if low <= words <= high:
        return result
    change = f"add about {low - words} words of the article's facts" if words < low else f"cut about {words - high} words"
    note = f"Word count: the summary has {words} words; it must be {state.summary_min_words}-{high} words ({change})."
    return result.model_copy(update={
        "is_valid": False,
        "format_check_passed": False,
        "feedback": f"{note} {result.feedback}".strip(),
    })


async def validate_summary(state: MainWorkflowState) -> MainWorkflowState:
    """
    Validates the generated summary against the original text.
    - Uses an LLM with structured output to populate ValidationResultModel.
    - This node's output (is_valid) will be used by the conditional edge to decide whether to loop or continue.
    - Records each attempt in the 'summary_attempts' list.
    """
    pprint("[NODE: VALIDATE SUMMARY] Starting validation...")

    # --- 0. FAIL FAST CHECK ---
    if state.error_message:
        return state

    try:
        # 1. Guards: Check if we have the necessary content
        if not state.cleaned_article_text:
            pprint("[NODE: VALIDATE SUMMARY] Error: cleaned_article_text is missing.")
            return state.model_copy(update={
                "error_message": "Cannot validate: cleaned_article_text is missing."
            })
        if not state.news_article or not state.news_article.summary:
            pprint("[NODE: VALIDATE SUMMARY] Error: summary is missing.")
            return state.model_copy(update={
                "error_message": "Cannot validate: summary is missing."
            })

        # 2. Get Prompts from State
        prompts = state.active_prompts
    
        # 3. Get the LLM
        model = settings.get_model()
        structured_llm = model.with_structured_output(ValidationResultModel)

        # 4. Format the prompt
        prompt = PromptTemplate.from_template(fill_lengths(prompts.validation_user, state))
        formatted_prompt = prompt.format(
            article_text=state.cleaned_article_text,
            summary_text=state.news_article.summary
        )

        # 5. Invoke the model
        messages = [
            ("system", fill_lengths(prompts.validation_system, state)),
            ("user", formatted_prompt)
        ]
        pprint("[NODE: VALIDATE SUMMARY] Invoking critic LLM...")
        validation_response: ValidationResultModel = await structured_llm.ainvoke(
            messages
        )
        pprint(validation_response.model_dump())

        # 5b. Word count, counted here: the critic LLM cannot count words reliably
        validation_response = check_word_count(validation_response, state)

        # 6. Record this attempt
        current_summary = state.news_article.summary
        new_attempt = SummaryAttemptModel(
            summary=current_summary,
            validation=validation_response
        )

        # Append to the list of all attempts
        updated_attempts_list = state.summary_attempts + [new_attempt]

        pprint(f"[NODE: VALIDATE SUMMARY] Attempt {len(updated_attempts_list)} recorded.")

        # 7. Update the state
        return state.model_copy(update={
            # Set the *latest* validation result for the conditional edge
            "validation_result": validation_response,
            # Store this attempt in our historical list
            "summary_attempts": updated_attempts_list,
            # Update validation_count based on list length
            "validation_count": len(updated_attempts_list)
        })

    except Exception as e:
        pprint(f"[NODE: VALIDATE SUMMARY] Error during validation: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in validate_summary: {e}"
        })
