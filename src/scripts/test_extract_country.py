import sys
import os
from pprint import pprint

# Setup path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from unittest.mock import MagicMock
sys.modules["tavily"] = MagicMock()

from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel
from src.models.AgentPromptsModel import AgentPromptsModel
from src.graph.nodes.extract_country import extract_country

def test_extract_country():
    # 1. Create Mock Prompts
    mock_prompts = AgentPromptsModel(
        content_extractor="...",
        summary_system="...",
        summary_initial_user="...",
        summary_retry_user="...",
        validation_system="...",
        validation_user="...",
        relevance_system="...",
        relevance_user="...",
        search_system="...",
        search_user="...",
        categorization_system="...",
        categorization_user="...",
        seo_system="...",
        seo_user="...",
        translation_system="...",
        translation_user="...",
        
        # The ones that matter for this test
        country_extraction_system="""You are an expert in geographical entity extraction.
your task is to identify and extract the country or countries that the news article is primarily about.

RULES:
1. Extract only the countries that are central to the news story.
2. If the article mentions a city or state, extract the corresponding country.
3. If no specific country is relevant (e.g., general tech news), return an empty list.
4. Output must be a list of country names in English.
5. Do not include regions (like "Middle East") unless a specific country is not applicable.
6. Normalize country names (e.g., "UAE" -> "United Arab Emirates", "US" -> "United States").""",
        country_extraction_user="""Please extract the relevant countries from the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---CONTENT SNIPPET---
{content}"""
    )

    # 2. Create Mock State
    article = ArticleModel(
        title="Dubai Property Market Booms",
        content="The real estate market in Dubai, United Arab Emirates, is seeing a significant surge in demand.",
        summary="Dubai's real estate sector is growing rapidly with new developments.",
    )

    state = MainWorkflowState(
        source_url="http://example.com/dubai-news",
        cleaned_article_text=article.content,
        news_article=article,
        active_prompts=mock_prompts
    )

    # 3. Run the Node
    print("--- Running extract_country Node ---")
    new_state = extract_country(state)

    # 4. Check Results
    if new_state.error_message:
        print(f"FAILED with error: {new_state.error_message}")
        return

    print(f"Original Title: {new_state.news_article.title}")
    print(f"Extracted Countries: {new_state.news_article.countries}")

    assert "United Arab Emirates" in new_state.news_article.countries or "UAE" in new_state.news_article.countries
    print("\n[SUCCESS] Verification passed!")

if __name__ == "__main__":
    test_extract_country()
