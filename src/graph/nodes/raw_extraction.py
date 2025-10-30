from src.models.MainWorkflowState import MainWorkflowState
from bs4 import BeautifulSoup
import requests
from pprint import pprint

def raw_extraction(state: MainWorkflowState) -> MainWorkflowState:
    """
    Fetch raw textual content from the page at the provided URL and update the workflow state.

    - Validates that a `source_url` is present.
    - Uses `requests` with a timeout and a basic User-Agent to reduce blocks.
    - Parses HTML with BeautifulSoup and extracts visible text.
    - Returns a new `MainWorkflowState` instance with `raw_extraction_result` set.

    Parameters:
        state: The current workflow state containing at least `source_url`.

    Returns:
        An updated `MainWorkflowState` with `raw_extraction_result` populated on success,
        or with an error message on failure. The original state is preserved via copy.
    """

    url = state.source_url

    # Guard: ensure URL is provided before attempting a request.
    if not url or not isinstance(url, str) or not url.strip():
        return state.model_copy(update={
            "raw_extraction_result": "No source_url provided."
        })

    pprint(f"[DEBUG][RAW EXTRACTION] Fetching content from: {url}")  # Prefer a configured logger in production.
    try:
        response = requests.get(
            url,
            timeout=25,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                               " AppleWebKit/537.36 (KHTML, like Gecko)"
                               " Chrome/124.0.0.0 Safari/537.36"
            },
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        text_content = soup.get_text(separator=" ", strip=True)

        # Return a copy of the state with the extracted content.
        return state.model_copy(update={
            "raw_extraction_result": text_content
        })

    except requests.RequestException as e:
        # Return a copy of the state embedding the error context for downstream handling.
        return state.model_copy(update={
            "raw_extraction_result": f"Error fetching content from {url}: {e}"
        })