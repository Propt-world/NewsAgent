import traceback
from pprint import pprint
from bs4 import BeautifulSoup
from typing import List
from src.models.MainWorkflowState import MainWorkflowState
from src.models.EmbeddedLinkModel import EmbeddedLinkModel

def extract_links(state: MainWorkflowState) -> MainWorkflowState:
    """
    Parses the 'cleaned_article_html' to extract all embedded hyperlinks.

    Uses BeautifulSoup for reliable, fast, and cheap HTML parsing.
    Updates the 'embedded_links' list within the 'news_article' in the state.
    """
    pprint("[NODE: EXTRACT LINKS] Starting link extraction...")

    # 1. Get the clean HTML snippet from the state
    html_snippet = state.cleaned_article_html

    # 2. Guard: Check if there's any HTML to parse
    if not html_snippet:
        pprint("[NODE: EXTRACT LINKS] No HTML snippet found. Skipping.")
        # Return the state unchanged
        return state

    # 3. Guard: Check if the news_article model exists
    if not state.news_article:
        pprint("[NODE: EXTRACT LINKS] Error: ArticleModel is missing.")
        return state.model_copy(update={
            "error_message": "Link extractor ran before ArticleModel was initialized."
        })

    try:
        # 4. Parse the HTML snippet
        soup = BeautifulSoup(html_snippet, "lxml") # Using lxml parser
        links = soup.find_all('a')

        extracted_links: List[EmbeddedLinkModel] = []

        for link in links:
            href = link.get('href')
            text = link.get_text(strip=True)

            # 5. Filter out invalid or empty links
            if not href or not text:
                continue

            # 6. Get surrounding text as 'context' for the link
            # This is useful for the relevance check later.
            parent_text = link.parent.get_text(strip=True) if link.parent else text

            # (Optional) Basic URL cleaning/absolutizing could go here.
            # For now, we'll just store what we found.

            extracted_links.append(
                EmbeddedLinkModel(
                    hyperlink_text=text,
                    url=href,
                    context=parent_text # Save context for later relevance check
                )
            )

        pprint(f"[NODE: EXTRACT LINKS] Found {len(extracted_links)} valid links.")

        # 7. Update the existing ArticleModel with the new list of links
        updated_article = state.news_article.model_copy(update={
            "embedded_links": extracted_links
        })

        # 8. Return the new state
        return state.model_copy(update={
            "news_article": updated_article
        })

    except Exception as e:
        pprint(f"[NODE: EXTRACT LINKS] Error parsing links: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error during link extraction: {e}"
        })