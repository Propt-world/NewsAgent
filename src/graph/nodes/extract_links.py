import traceback
import re
from pprint import pprint
from bs4 import BeautifulSoup
from typing import List, Optional
from urllib.parse import urljoin, urlparse
from src.models.MainWorkflowState import MainWorkflowState
from src.models.EmbeddedLinkModel import EmbeddedLinkModel

# --- NEW: Helper function for filtering ---

# Block common ad, tracker, and social media domains
DOMAIN_BLOCKLIST = [
    "doubleclick.net", "googleadservices.com", "googlesyndication.com",
    "adservice.google.com", "analytics.google.com", "facebook.com",
    "twitter.com", "linkedin.com", "instagram.com", "pinterest.com",
    "ad.doubleclick.net", "c.ad.doubleclick.net", "platform.twitter.com",
    "syndication.twitter.com", "adobedtm.com", "omtrdc.net", "outbrain.com",
    "taboola.com", "sharethrough.com", "adsrvr.org"
]

# Block common link text patterns (case-insensitive)
TEXT_BLOCKLIST_PATTERNS = [
    r"^share$", r"^tweet$", r"^post$", r"^facebook$", r"^twitter$",
    r"^linkedin$", r"^pinterest$", r"^advertisement$", r"^related:$",
    r"share on.*", r"share to.*"
]

def _is_valid_link(href: str, text: str, base_domain: str) -> bool:
    """
    Checks if a link is likely a real, relevant link and not
    an ad, social media button, or internal jump.
    """
    if not href or not text:
        return False

    try:
        parsed_url = urlparse(href)

        # 1. Filter out internal page jumps (e.g., #comments)
        if not parsed_url.scheme and not parsed_url.netloc and href.startswith('#'):
            return False

        # 2. Filter out 'javascript:void(0)' or mailto links
        if parsed_url.scheme in ['javascript', 'mailto']:
            return False

        # 3. Filter based on link text
        for pattern in TEXT_BLOCKLIST_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return False

        # 4. Filter based on domain
        domain = parsed_url.netloc

        # If it's a relative link, domain will be empty.
        # We only check absolute links against the blocklist.
        if domain:
            # Check against main blocklist
            if any(block in domain for block in DOMAIN_BLOCKLIST):
                return False

            # Filter links that just link back to the same domain (e.g., "Related")
            # We can be less strict here, but it's an option.
            # if domain == base_domain:
            #    return False

        return True

    except Exception:
        # Bad URL, e.g. unparseable
        return False

# --- Updated Node ---

def extract_links(state: MainWorkflowState) -> MainWorkflowState:
    """
    Parses 'cleaned_article_html', *filters* out irrelevant links,
    and extracts all valid, absolute URLs.
    """
    pprint("[NODE: EXTRACT LINKS] Starting link extraction & filtering...")

    html_snippet = state.cleaned_article_html
    base_url = state.source_url

    if not html_snippet or not base_url:
        pprint("[NODE: EXTRACT LINKS] No HTML snippet or base URL. Skipping.")
        return state

    if not state.news_article:
        return state.model_copy(update={
            "error_message": "Link extractor ran before ArticleModel was initialized."
        })

    try:
        soup = BeautifulSoup(html_snippet, "lxml")
        links = soup.find_all('a')

        # Get the domain of the source article for comparison
        base_domain = urlparse(base_url).netloc

        extracted_links: List[EmbeddedLinkModel] = []

        for link in links:
            href = link.get('href')
            text = link.get_text(strip=True)

            # --- 5. NEW: Filtering step ---
            if not _is_valid_link(href, text, base_domain):
                continue

            # --- 6. NEW: Resolve relative URLs ---
            # Converts "/p/my-article" into "https://site.com/p/my-article"
            absolute_url = urljoin(base_url, href)

            parent_text = link.parent.get_text(strip=True) if link.parent else text

            extracted_links.append(
                EmbeddedLinkModel(
                    hyperlink_text=text,
                    url=absolute_url, # <-- Store the absolute URL
                    context=parent_text
                )
            )

        pprint(f"[NODE: EXTRACT LINKS] Found {len(extracted_links)} valid/filtered links.")

        updated_article = state.news_article.model_copy(update={
            "embedded_links": extracted_links
        })

        return state.model_copy(update={
            "news_article": updated_article
        })

    except Exception as e:
        pprint(f"[NODE: EXTRACT LINKS] Error parsing links: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"Error during link extraction: {e}"
        })