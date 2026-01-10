import traceback
from pprint import pprint
from newspaper import Article
from lxml.html import tostring
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel
from src.configs.settings import settings
from src.utils.browser import get_async_browser_context
from src.utils.governance import GovernanceGatekeeper

# --- CONFIG: RESOURCE BLOCKING ---
# Block heavy resources that crash headless browsers
BLOCKED_RESOURCE_TYPES = ["image", "media", "font", "stylesheet"] 
# Block known ad/tracker domains
BLOCKED_URL_PATTERNS = [
    "doubleclick", "googlead", "googlesyndication", "adservice",
    "analytics", "facebook", "twitter", "outbrain", "taboola", 
    "adsrvr", "rubicon", "criteo", "amazon-adsystem"
]

async def raw_extraction(state: MainWorkflowState) -> MainWorkflowState:
    """
    Extracts article content using Playwright (Async) and Newspaper4k.
    Includes Ad-Blocking and Lazy-Load scrolling for stability.
    """
    
    # --- 0. GOVERNANCE CHECK ---
    url = state.source_url
    gatekeeper = GovernanceGatekeeper()

    if not gatekeeper.can_fetch(url):
        pprint(f"[NODE: RAW EXTRACTION] 🛑 Blocked by robots.txt: {url}")
        return state.model_copy(update={
            "error_message": f"Blocked by robots.txt: {url}"
        })

    gatekeeper.wait_for_slot(url)
    
    pprint(f"[NODE: RAW EXTRACTION] 🚀 Fetching with Playwright (Async): {url}")

    try:
        async with get_async_browser_context() as (playwright, browser):
            page = await browser.new_page(user_agent=settings.USER_AGENT)

            # --- 1. NETWORK INTERCEPTION (AD BLOCKER) ---
            # This is critical for stability on heavy news sites
            async def route_handler(route):
                request = route.request
                # Block by resource type (optional, but saves memory)
                if request.resource_type in BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                    return
                # Block by domain pattern
                if any(pattern in request.url for pattern in BLOCKED_URL_PATTERNS):
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", route_handler)

            # --- 2. NAVIGATION & LAZY LOADING ---
            try:
                # 'domcontentloaded' is faster and safer since we manually wait later
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                
                # Scroll Logic (Triggers lazy loading of text)
                # We prioritize text visibility over images/ads
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await page.wait_for_timeout(1000)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000) 

            except Exception as e:
                # If navigation has issues but page is open, we try to proceed
                pprint(f"[NODE: RAW EXTRACTION] Navigation warning: {e}")

            # --- 3. EXTRACTION ---
            # We explicitly check if page is closed to avoid TargetClosedError
            if page.is_closed():
                raise Exception("Browser page crashed or closed unexpectedly during navigation.")

            html_content = await page.content()
            page_title = await page.title()
            
            # --- 4. PARSING (Newspaper4k) ---
            article = Article(url)
            article.download(input_html=html_content)
            article.parse()

            # --- 5. QUALITY CHECK ---
            if not article.text or len(article.text) < 50:
                print(f"\n--- ❌ EXTRACTION FAILED DEBUG INFO ---")
                print(f"URL: {url}")
                print(f"Title: {page_title}")
                print(f"HTML Size: {len(html_content)}")
                print("---------------------------------------\n")
                
                return state.model_copy(update={
                    "error_message": f"Extracted content is empty. Page Title: '{page_title}'"
                })

            # --- 6. SUCCESS ---
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