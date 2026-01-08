import math
from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState

def calculate_reading_time(state: MainWorkflowState) -> MainWorkflowState:
    """
    Node: CALCULATE READING TIME

    Responsibilities:
    1. Calculate reading time for English content (200 wpm)
    2. Calculate reading time for Arabic content (200 wpm)
    3. Update the ArticleModel independently
    """
    pprint("[NODE: CALC READING TIME] Starting reading time calculation...")

    if not state.news_article:
        pprint("[NODE: CALC READING TIME] Error: news_article is missing.")
        return state

    WPM = 200
    updates = {}

    # 1. English Reading Time
    # We prefer to use the cleaned_article_text for calculation as it represents the full article
    # Fallback to content if cleaned_text is somehow missing
    english_text = state.cleaned_article_text or state.news_article.content
    if english_text:
        word_count = len(english_text.split())
        reading_time = math.ceil(word_count / WPM)
        updates["reading_time"] = reading_time
        pprint(f"[NODE: CALC READING TIME] English: {word_count} words -> {reading_time} min")

    # 2. Arabic Reading Time
    arabic_text = state.news_article.content_ar
    if arabic_text:
        word_count_ar = len(arabic_text.split())
        reading_time_ar = math.ceil(word_count_ar / WPM)
        updates["reading_time_ar"] = reading_time_ar
        pprint(f"[NODE: CALC READING TIME] Arabic: {word_count_ar} words -> {reading_time_ar} min")

    if updates:
        updated_article = state.news_article.model_copy(update=updates)
        return state.model_copy(update={"news_article": updated_article})

    pprint("[NODE: CALC READING TIME] No updates made.")
    return state
