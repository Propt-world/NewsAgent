import traceback
import json
from pprint import pprint
from newspaper import Article
from lxml.html import tostring
from bs4 import BeautifulSoup 
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel
from src.configs.settings import settings
from src.utils.browser import get_async_browser_context
from src.utils.governance import GovernanceGatekeeper

# --- CONFIG: RESOURCE BLOCKING ---
BLOCKED_RESOURCE_TYPES = ["image", "media", "font", "stylesheet"] 
BLOCKED_URL_PATTERNS = [
    "doubleclick", "googlead", "googlesyndication", "adservice",
    "analytics", "facebook", "twitter", "outbrain", "taboola", 
    "adsrvr", "rubicon", "criteo", "amazon-adsystem"
]

async def raw_extraction(state: MainWorkflowState) -> MainWorkflowState:
    """
    Extracts article content using Playwright (Async).
    Prioritizes:
    1. Newspaper4k (Standard)
    2. JSON-LD Structured Data (High Accuracy)
    3. Manual BeautifulSoup Fallback (Specific Selectors)
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
            async def route_handler(route):
                request = route.request
                if request.resource_type in BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                    return
                if any(pattern in request.url for pattern in BLOCKED_URL_PATTERNS):
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", route_handler)

            # --- 2. NAVIGATION & LAZY LOADING ---
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                
                # Scroll Logic (Triggers lazy loading)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await page.wait_for_timeout(1000)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000) 

            except Exception as e:
                pprint(f"[NODE: RAW EXTRACTION] Navigation warning: {e}")

            if page.is_closed():
                raise Exception("Browser page crashed or closed unexpectedly.")

            html_content = await page.content()
            page_title = await page.title()
            
            # --- 3. STRATEGY A: Newspaper4k ---
            article = Article(url)
            article.download(input_html=html_content)
            article.parse()
            
            extracted_text = article.text
            source_strategy = "Newspaper4k"

            # --- 4. STRATEGY B: JSON-LD (Structured Data) ---
            # This is highly effective for Gulf News and modern sites
            if not extracted_text or len(extracted_text) < 200:
                print(f"[NODE: RAW EXTRACTION] ⚠️ Newspaper text empty/short. Checking JSON-LD...")
                soup = BeautifulSoup(html_content, "lxml")
                scripts = soup.find_all('script', type='application/ld+json')
                
                for script in scripts:
                    try:
                        data = json.loads(script.string)
                        # JSON-LD can be a list or a single object
                        if isinstance(data, list):
                            items = data
                        else:
                            items = [data]

                        for item in items:
                            # Check for 'articleBody' in NewsArticle or Article types
                            if 'articleBody' in item:
                                clean_body = item['articleBody']
                                # Basic cleaning of HTML entities if present
                                clean_body = BeautifulSoup(clean_body, "lxml").get_text()
                                
                                if len(clean_body) > 200:
                                    extracted_text = clean_body
                                    source_strategy = "JSON-LD"
                                    print(f"[NODE: RAW EXTRACTION] ✅ Success via JSON-LD (Length: {len(extracted_text)})")
                                    break
                        if extracted_text and len(extracted_text) > 200: break
                    except:
                        continue

            # --- 5. STRATEGY C: Manual Selectors (BS4) ---
            if not extracted_text or len(extracted_text) < 200:
                print(f"[NODE: RAW EXTRACTION] ⚠️ JSON-LD failed. Attempting Manual Selectors...")
                soup = BeautifulSoup(html_content, "lxml")
                
                # Selectors specific to Gulf News and general fallbacks
                selectors = [
                    "div.story-element-text",  # Gulf News Specific
                    ".story-element",          # Gulf News Specific
                    ".Iqx1L",                  # Gulf News Obscure Class
                    "article", 
                    ".story-content", 
                    ".article-body", 
                    "#article-body", 
                    ".post-content",
                    "main"
                ]
                
                for selector in selectors:
                    elements = soup.select(selector)
                    if elements:
                        # Join all found elements (Gulf News splits text into multiple divs)
                        text_parts = [e.get_text(separator=" ", strip=True) for e in elements]
                        full_text = "\n\n".join(text_parts)
                        
                        if len(full_text) > 200:
                            extracted_text = full_text
                            source_strategy = f"BS4: {selector}"
                            print(f"[NODE: RAW EXTRACTION] ✅ Success via Manual Selector: '{selector}'")
                            break

            # --- 6. FINAL QUALITY CHECK ---
            if not extracted_text or len(extracted_text) < 50:
                print(f"\n--- ❌ EXTRACTION FAILED DEBUG INFO ---")
                print(f"URL: {url}")
                print(f"Title: {page_title}")
                print(f"HTML Size: {len(html_content)}")
                print("---------------------------------------\n")
                
                return state.model_copy(update={
                    "error_message": f"Extracted content is empty. Page Title: '{page_title}'"
                })

            # --- 7. SUCCESS ---
            initial_article = ArticleModel(
                title=article.title or page_title,
                content=extracted_text,
                published_date=article.publish_date.isoformat() if article.publish_date else None,
                author=", ".join(article.authors) if article.authors else None,
                top_image=article.top_image
            )
            
            print(f"[NODE: RAW EXTRACTION] ✅ Final Success using: {source_strategy}")

            clean_html = ""
            if article.top_node is not None:
                clean_html = tostring(article.top_node, encoding='unicode')

            return state.model_copy(update={
                "cleaned_article_text": extracted_text,
                "cleaned_article_html": clean_html,
                "news_article": initial_article
            })

    except Exception as e:
        pprint(f"[NODE: RAW EXTRACTION] Critical Error: {e}")
        traceback.print_exc()
        return state.model_copy(update={"error_message": f"Playwright Error: {e}"})