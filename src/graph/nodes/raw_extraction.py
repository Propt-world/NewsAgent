import traceback
from pprint import pprint
from newspaper import Article
from lxml.html import tostring
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel
from src.utils.browser import get_sync_browser_context

def raw_extraction(state: MainWorkflowState) -> MainWorkflowState:
    """
    Extracts article content using Playwright (Sync) and Newspaper4k.
    
    Improvement over requests-html:
    - Uses Playwright's robust engine which doesn't "leak" processes.
    - Waits intelligently for the DOM to settle (domcontentloaded).
    """
    url = state.source_url
    pprint(f"[NODE: RAW EXTRACTION] 🚀 Fetching with Playwright: {url}")

    playwright = None
    browser = None
    
    try:
        # --- 1. BROWSER INITIALIZATION ---
        # We start a fresh browser instance for this job.
        # This guarantees clean state (no cookies/cache from previous jobs).
        playwright, browser = get_sync_browser_context()
        page = browser.new_page()

        # --- 2. NAVIGATION & WAIT ---
        try:
            # We assume a 60s timeout. EC2 networks can sometimes be slow/throttled.
            # wait_until='domcontentloaded' waits for the HTML to be parsed and DOM built.
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            
            # OPTIONAL: Wait a tiny bit extra (2s) for "hydration"
            # (e.g., React apps that attach text after the initial HTML load).
            page.wait_for_timeout(2000) 
        except Exception as e:
            # If navigation times out, we DON'T fail yet. 
            # Often the text is already loaded, but some tracking pixel is stalling the page.
            pprint(f"[NODE: RAW EXTRACTION] Navigation warning (attempting extract anyway): {e}")

        # --- 3. EXTRACTION ---
        # Get the full, rendered HTML from the browser
        html_content = page.content()
        
        # --- 4. PARSING (Newspaper4k) ---
        # We feed the rendered HTML into Newspaper4k.
        # This allows us to use Newspaper's excellent logic on JS-heavy sites.
        article = Article(url)
        article.download(input_html=html_content)
        article.parse()

        # --- 5. QUALITY CHECK ---
        # If the text is empty or trivially short, something went wrong.
        if not article.text or len(article.text) < 50:
             return state.model_copy(update={
                "error_message": "Extracted content is empty or too short."
            })

        # --- 6. BUILD MODEL ---
        # Create the data object for the rest of the workflow
        initial_article = ArticleModel(
            title=article.title,
            content=article.text,
            published_date=article.publish_date.isoformat() if article.publish_date else None,
            author=", ".join(article.authors) if article.authors else None,
            top_image=article.top_image
        )

        # We also keep the raw HTML of the main node for the 'extract_links' node
        clean_html = ""
        if article.top_node is not None:
            clean_html = tostring(article.top_node, encoding='unicode')

        return state.model_copy(update={
            "cleaned_article_text": article.text,
            "cleaned_article_html": clean_html,
            "news_article": initial_article
        })

    except Exception as e:
        # Catch-all for unexpected browser crashes
        pprint(f"[NODE: RAW EXTRACTION] Critical Error: {e}")
        traceback.print_exc()
        return state.model_copy(update={"error_message": f"Playwright Error: {e}"})
        
    finally:
        # --- 7. CLEANUP (CRITICAL) ---
        # We MUST close the browser here. If we don't, the Chromium process
        # will stay alive as a "zombie" and eat up your RAM.
        if browser: browser.close()
        if playwright: playwright.stop()