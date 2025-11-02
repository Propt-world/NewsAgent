from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState

def check_summary_validity(state: MainWorkflowState) -> str:
    """
    Conditional edge to decide if we should retry summary
    generation or proceed.
    """
    pprint("[EDGE: CHECK VALIDITY] Checking validation status...")

    validation_result = state.validation_result
    validation_count = state.validation_count
    max_retries = state.max_retries

    if not validation_result:
        pprint("[EDGE: CHECK VALIDITY] No validation result. Stopping.")
        return "end_loop" # Safety check

    # 1. Check for a passing score
    if validation_result.is_valid:
        pprint("[EDGE: CHECK VALIDITY] Summary is valid. Ending loop.")
        return "end_loop"

    # 2. Check if we've hit max retries
    if validation_count >= max_retries:
        pprint(f"[EDGE: CHECK VALIDITY] Max retries ({max_retries}) reached. Ending loop.")
        return "end_loop"

    # 3. If not valid and not at max retries, try again
    pprint(f"[EDGE: CHECK VALIDITY] Summary invalid. Retrying (Attempt {validation_count + 1}).")
    return "regenerate"