import traceback
from pprint import pprint
from newspaper import Article
from lxml.html import tostring
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel
from src.configs.settings import settings
from src.utils.browser import get_async_browser_context
from src.utils.governance import GovernanceGatekeeper

async def raw_extraction(state: MainWorkflowState) -> MainWorkflowState:
    """
    Extracts article content using Playwright (Async) and Newspaper4k.
    """
    
    # --- 0. GOVERNANCE CHECK ---
    url = state.source_url
    gatekeeper = GovernanceGatekeeper()

    # A. Check Robots.txt
    if not gatekeeper.can_fetch(url):
        pprint(f"[NODE: RAW EXTRACTION] 🛑 Blocked by robots.txt: {url}")
        return state.model_copy(update={
            "error_message": f"Blocked by robots.txt: {url}"
        })

    # B. Rate Limit (Block until safe)
    gatekeeper.wait_for_slot(url)
    
    pprint(f"[NODE: RAW EXTRACTION] 🚀 Fetching with Playwright (Async): {url}")

    try:
        # --- 1. BROWSER INITIALIZATION ---
        async with get_async_browser_context() as (playwright, browser):
            # Pass User-Agent to avoid default headless Chrome UA
            page = await browser.new_page(user_agent=settings.USER_AGENT)

            # --- 2. NAVIGATION & WAIT ---
            try:
                # Use 'networkidle' to wait for initial XHR/API calls to finish
                await page.goto(url, timeout=60000, wait_until="networkidle")
                
                # --- NEW: LAZY LOAD SCROLL ---
                # Some sites hide the rest of the article until you scroll.
                # 1. Scroll halfway
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await page.wait_for_timeout(1000)
                
                # 2. Scroll to bottom
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000) # Give it time to render
                
            except Exception as e:
                pprint(f"[NODE: RAW EXTRACTION] Navigation/Scroll warning: {e}")

            # --- 3. EXTRACTION ---
            html_content = await page.content()
            page_title = await page.title() # Capture title for debugging
            
            # --- 4. PARSING (Newspaper4k) ---
            article = Article(url)
            article.download(input_html=html_content)
            article.parse()

            # --- 5. QUALITY CHECK & DEBUGGING ---
            if not article.text or len(article.text) < 50:
                print(f"\n--- ❌ EXTRACTION FAILED DEBUG INFO ---")
                print(f"URL: {url}")
                print(f"Page Title: {page_title}")
                print(f"HTML Length: {len(html_content)} chars")
                print(f"Snippet: {article.text[:100] if article.text else 'NO TEXT'}")
                print(f"---------------------------------------\n")
                
                return state.model_copy(update={
                    "error_message": f"Extracted content is empty or too short. Page Title: '{page_title}'"
                })

            # --- 6. BUILD MODEL ---
            initial_article = ArticleModel(
                title=article.title,
                content=article.text,
                published_date=article.publish_date.isoformat() if article.publish_date else None,
                author=", ".join(article.authors) if article.authors else None,
                top_image=article.top_image
            )

            clean_html = ""
            if article.top_node is not None:
                clean_html = tostring(article.top_node, encoding='unicode')

            return state.model_copy(update={
                "cleaned_article_text": article.text,
                "cleaned_article_html": clean_html,
                "news_article": initial_article
            })

    except Exception as e:
        pprint(f"[NODE: RAW EXTRACTION] Critical Error: {e}")
        traceback.print_exc()
        return state.model_copy(update={"error_message": f"Playwright Error: {e}"})