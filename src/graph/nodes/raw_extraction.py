import traceback
import json
import re
from pprint import pprint
from newspaper import Article
from lxml.html import tostring
from bs4 import BeautifulSoup 
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel
from src.configs.settings import settings
from src.utils.browser import get_async_browser_context
from src.utils.governance import AsyncGovernanceGatekeeper

# --- CONFIG: RESOURCE BLOCKING ---
BLOCKED_RESOURCE_TYPES = ["image", "media", "font", "stylesheet"] 
BLOCKED_URL_PATTERNS = [
    "doubleclick", "googlead", "googlesyndication", "adservice",
    "analytics", "facebook", "twitter", "outbrain", "taboola", 
    "adsrvr", "rubicon", "criteo", "amazon-adsystem"
]

# --- CONFIG: COOKIE / CONSENT POP-UPS ---
# A page read before anyone accepts cookies often carries a consent pop-up; Newspaper4k can mistake it for the
# article (zawya, 2026-10-29: the whole body and summary became the cookie policy). Pop-ups are removed with the
# other clutter, and text that still reads like a consent notice is never used as the article.
CONSENT_SELECTORS = [
    "#onetrust-consent-sdk", "#onetrust-banner-sdk", "#onetrust-pc-sdk", "#CybotCookiebotDialog",
    "#didomi-host", ".qc-cmp2-container", ".fc-consent-root", "#usercentrics-root",
    "[id*='cookie' i]", "[class*='cookie' i]", "[id*='consent' i]", "[class*='consent' i]",
    "[aria-label*='cookie' i]", "[aria-label*='consent' i]",
]
# A wrapper whose name happens to contain "cookie"/"consent" must not take the article with it
CONSENT_ELEMENT_MAX_CHARS = 6000
CONSENT_WORDS = re.compile(r"\bcookies?\b|\bconsent\b|tracking technolog|\bopt[- ]out\b|privacy (?:preferences|choices|settings)",
                           re.IGNORECASE)
CONSENT_NOTICE_MESSAGE = "The page returned a cookie/consent notice instead of the article"


def remove_consent_elements(soup: BeautifulSoup) -> int:
    """Removes cookie / consent pop-ups from the page before it is read. Returns how many were removed."""
    removed = 0
    for tag in soup.select(", ".join(CONSENT_SELECTORS)):
        if tag.decomposed:
            continue
        if len(tag.get_text(" ", strip=True)) <= CONSENT_ELEMENT_MAX_CHARS:
            tag.decompose()
            removed += 1
    return removed


def is_consent_text(text: str) -> bool:
    """True when the text is mostly a cookie / consent notice: at least half of its paragraphs are about cookies or
    consent, and they say so several times. An article that only mentions cookies in passing is kept."""
    paragraphs = [p for p in re.split(r"\n+", text or "") if len(p.strip()) >= 20]
    if not paragraphs:
        return False
    about_consent = [p for p in paragraphs if CONSENT_WORDS.search(p)]
    mentions = sum(len(CONSENT_WORDS.findall(p)) for p in paragraphs)
    return len(about_consent) / len(paragraphs) >= 0.5 and mentions >= 3


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
    gatekeeper = AsyncGovernanceGatekeeper()

    if not await gatekeeper.can_fetch(url):
        pprint(f"[NODE: RAW EXTRACTION] 🛑 Blocked by robots.txt: {url}")
        return state.model_copy(update={
            "error_message": f"Blocked by robots.txt: {url}"
        })

    await gatekeeper.wait_for_slot(url)
    
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

                # Handle Cloudflare/Anti-Bot "Checking your browser" screens
                try:
                    current_title = await page.title()
                    if "checking your browser" in current_title.lower() or "just a moment" in current_title.lower():
                        print(f"[NODE: RAW EXTRACTION] 🛡️ Anti-bot screen detected: '{current_title}'. Waiting for redirect...")
                        
                        # Wait up to 15 seconds for an <h1> tag (indicating real content loaded)
                        try:
                            await page.wait_for_selector("h1", state="attached", timeout=15000)
                            print("[NODE: RAW EXTRACTION] ✅ Redirect successful. Real content loaded.")
                        except Exception:
                            print("[NODE: RAW EXTRACTION] ⚠️ Timed out waiting for H1. Proceeding anyway...")
                except Exception as e:
                    print(f"[NODE: RAW EXTRACTION] Warning checking title: {e}")
                
                # Scroll Logic
                await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                await page.wait_for_timeout(2000) 

            except Exception as e:
                pprint(f"[NODE: RAW EXTRACTION] Navigation warning: {e}")

            # --- REPLACEMENT START ---
            if page.is_closed():
                raise Exception("Browser page crashed or closed unexpectedly.")

            # --- RETRY LOGIC FOR CONTENT RETRIEVAL ---
            html_content = ""
            for attempt in range(3):
                try:
                    html_content = await page.content()
                    break
                except Exception as e:
                    if "navigating" in str(e) or "Execution context was destroyed" in str(e):
                        print(f"[NODE: RAW EXTRACTION] Navigation detected during read. Retrying {attempt+1}/3...")
                        await page.wait_for_timeout(1000)
                    else:
                        raise e
            
            if not html_content:
                 raise Exception("Failed to retrieve content after retries.")

            page_title = await page.title()

            # --- NEW: HTML PRE-CLEANING & EXACT TITLE EXTRACTION ---
            soup = BeautifulSoup(html_content, "lxml")
            
            # 1. Grab bulletproof title from Meta Tags before parsing libraries get confused
            og_title = soup.find("meta", property="og:title")
            bulletproof_title = og_title["content"] if og_title and og_title.get("content") else page_title
            
            # 2. Decompose sidebars and recommended sections to prevent "Content Bleed"
            noise_selectors = [
                "aside", "footer", "nav", "header", ".sidebar", 
                ".article-right-sidebar", ".related", ".recommended", 
                ".most-popuplar-ongoing-viral-outer", ".footer-menu"
            ]
            for tag in soup.select(", ".join(noise_selectors)):
                tag.decompose()
            if remove_consent_elements(soup):
                print("[NODE: RAW EXTRACTION] 🍪 Removed cookie/consent pop-up(s) before reading.")
                
            cleaned_html_for_parsing = str(soup)
            # --- REPLACEMENT END ---
            
            # --- 3. STRATEGY A: Newspaper4k ---
            extracted_text = ""
            source_strategy = ""
            clean_html = cleaned_html_for_parsing
            
            pub_date = None
            authors_str = None
            top_img = None
            
            try:
                # --- FIX: Force English to ignore site typos like <html lang="bn"> ---
                article = Article(url, language='en') 
                
                article.download(input_html=cleaned_html_for_parsing)
                article.parse()
                
                extracted_text = article.text
                source_strategy = "Newspaper4k"
                
                if article.top_node is not None:
                    clean_html = tostring(article.top_node, encoding='unicode')
                    
                pub_date = article.publish_date.isoformat() if article.publish_date else None
                authors_str = ", ".join(article.authors) if article.authors else None
                top_img = article.top_image
                
            except Exception as parse_error:
                print(f"[NODE: RAW EXTRACTION] ⚠️ Newspaper4k failed ({parse_error}). Falling back...")
                pass

            # A consent notice is never the article: try the other ways instead
            consent_seen = is_consent_text(extracted_text)
            if consent_seen:
                print("[NODE: RAW EXTRACTION] 🍪 Newspaper4k text is a cookie/consent notice. Trying the other ways...")
                extracted_text = ""

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
                                if is_consent_text(clean_body):
                                    consent_seen = True
                                    continue
                                
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
                remove_consent_elements(soup)
                
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
                        if is_consent_text(full_text):
                            consent_seen = True
                            continue
                        
                        if len(full_text) > 200:
                            extracted_text = full_text
                            source_strategy = f"BS4: {selector}"
                            print(f"[NODE: RAW EXTRACTION] ✅ Success via Manual Selector: '{selector}'")
                            break

            # --- 6. FINAL QUALITY CHECK ---
            if consent_seen and (not extracted_text or len(extracted_text) < 50):
                # Only the consent notice was found: stop here rather than write an article about cookies
                print(f"[NODE: RAW EXTRACTION] 🍪 {CONSENT_NOTICE_MESSAGE}: {url}")
                return state.model_copy(update={
                    "error_message": f"{CONSENT_NOTICE_MESSAGE}. Page Title: '{page_title}'"
                })

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
                title=bulletproof_title, 
                content=extracted_text,
                source_title=bulletproof_title,
                published_date=pub_date,     # <--- FIX: Use the safe variable
                author=authors_str,          # <--- FIX: Use the safe variable
                top_image=top_img            # <--- FIX: Use the safe variable
            )
            
            print(f"[NODE: RAW EXTRACTION] ✅ Final Success using: {source_strategy}")

            # Remove the clean_html assignment here since we already handled it safely above!

            return state.model_copy(update={
                "cleaned_article_text": extracted_text,
                "cleaned_article_html": clean_html, # clean_html is safe because we defined it on line 121
                "news_article": initial_article
            })

    except Exception as e:
        pprint(f"[NODE: RAW EXTRACTION] Critical Error: {e}")
        traceback.print_exc()
        return state.model_copy(update={"error_message": f"Playwright Error: {e}"})
