import traceback
from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState

def select_best_summary(state: MainWorkflowState) -> MainWorkflowState:
    """
    Selects the best summary from the 'summary_attempts' list
    based on the highest 'semantic_score'.

    This node runs *after* the validation loop is complete.
    """
    pprint("[NODE: SELECT BEST SUMMARY] Selecting best summary...")

    # --- 0. FAIL FAST CHECK ---
    if state.error_message:
        return state

    try:
        all_attempts = state.summary_attempts

        if not all_attempts:
            pprint("[NODE: SELECT BEST SUMMARY] Error: No summaries were generated.")
            return state.model_copy(update={
                "error_message": "No summaries to select from."
            })

        # 1. Find the best attempt
        # We use 'semantic_score or 0.0' to handle None values
        best_attempt = max(
            all_attempts,
            key=lambda attempt: attempt.validation.semantic_score or 0.0
        )

        best_summary = best_attempt.summary
        best_validation = best_attempt.validation

        pprint(f"[NODE: SELECT BEST SUMMARY] Best summary found (Attempt with {best_validation.semantic_score} semantic score).")

        # 2. Update the final 'news_article'
        updated_article = state.news_article.model_copy(update={
            "summary": best_summary
        })

        # 3. Update the state
        return state.model_copy(update={
            "news_article": updated_article,
            # Set the final validation_result to the one from the best attempt
            "validation_result": best_validation
        })

    except Exception as e:
        pprint(f"[NODE: SELECT BEST SUMMARY] Error selecting best summary: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error in select_best_summary: {e}"
        })