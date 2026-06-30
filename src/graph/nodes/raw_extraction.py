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

# Fallback UA used only when normal extraction returns empty content.
CRAWLER_USER_AGENT = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)

WAF_CHALLENGE_MARKERS = [
    "verification required",
    "request could not be satisfied",
    "detected unusual activity",
    "automated (bot) activity",
    "captcha",
    "cf-chl",
    "cloudflare",
    "attention required",
    "why is this step needed",
]


def is_cdn_waf_challenge_page(page_title: str, html_content: str) -> bool:
    """Detects common CDN/WAF challenge pages from title/body signatures."""
    title = (page_title or "").lower()
    # Limit scan length for performance while keeping enough signal.
    body = (html_content or "")[:4000].lower()
    combined = f"{title}\n{body}"
    return any(marker in combined for marker in WAF_CHALLENGE_MARKERS)

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

            # --- 5.5 ONE-TIME FALLBACK: CRAWLER USER AGENT ---
            if not extracted_text or len(extracted_text) < 50:
                print(
                    "[NODE: RAW EXTRACTION] ⚠️ Primary extraction returned empty content. "
                    "Retrying once with crawler user-agent..."
                )
                crawler_page = await browser.new_page(user_agent=CRAWLER_USER_AGENT)
                await crawler_page.route("**/*", route_handler)

                try:
                    try:
                        await crawler_page.goto(url, timeout=60000, wait_until="domcontentloaded")
                        await crawler_page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                        await crawler_page.wait_for_timeout(2000)
                    except Exception as nav_error:
                        pprint(f"[NODE: RAW EXTRACTION] Crawler fallback navigation warning: {nav_error}")

                    crawler_html = ""
                    for attempt in range(3):
                        try:
                            crawler_html = await crawler_page.content()
                            break
                        except Exception as read_error:
                            if "navigating" in str(read_error) or "Execution context was destroyed" in str(read_error):
                                print(
                                    "[NODE: RAW EXTRACTION] Crawler fallback navigation during read. "
                                    f"Retrying {attempt+1}/3..."
                                )
                                await crawler_page.wait_for_timeout(1000)
                            else:
                                raise read_error

                    crawler_title = await crawler_page.title()

                    if crawler_html:
                        crawler_soup = BeautifulSoup(crawler_html, "lxml")
                        crawler_og_title = crawler_soup.find("meta", property="og:title")
                        crawler_bulletproof_title = (
                            crawler_og_title["content"]
                            if crawler_og_title and crawler_og_title.get("content")
                            else crawler_title
                        )

                        noise_selectors = [
                            "aside", "footer", "nav", "header", ".sidebar",
                            ".article-right-sidebar", ".related", ".recommended",
                            ".most-popuplar-ongoing-viral-outer", ".footer-menu"
                        ]
                        for tag in crawler_soup.select(", ".join(noise_selectors)):
                            tag.decompose()

                        crawler_cleaned_html = str(crawler_soup)

                        crawler_text = ""
                        crawler_strategy = ""
                        crawler_clean_html = crawler_cleaned_html
                        crawler_pub_date = None
                        crawler_authors = None
                        crawler_top_img = None

                        try:
                            crawler_article = Article(url, language='en')
                            crawler_article.download(input_html=crawler_cleaned_html)
                            crawler_article.parse()

                            crawler_text = crawler_article.text
                            crawler_strategy = "Newspaper4k"

                            if crawler_article.top_node is not None:
                                crawler_clean_html = tostring(crawler_article.top_node, encoding='unicode')

                            crawler_pub_date = (
                                crawler_article.publish_date.isoformat()
                                if crawler_article.publish_date
                                else None
                            )
                            crawler_authors = ", ".join(crawler_article.authors) if crawler_article.authors else None
                            crawler_top_img = crawler_article.top_image
                        except Exception:
                            pass

                        if not crawler_text or len(crawler_text) < 200:
                            scripts = crawler_soup.find_all('script', type='application/ld+json')
                            for script in scripts:
                                try:
                                    data = json.loads(script.string)
                                    items = data if isinstance(data, list) else [data]

                                    for item in items:
                                        if 'articleBody' in item:
                                            clean_body = BeautifulSoup(item['articleBody'], "lxml").get_text()
                                            if len(clean_body) > 200:
                                                crawler_text = clean_body
                                                crawler_strategy = "JSON-LD"
                                                break
                                    if crawler_text and len(crawler_text) > 200:
                                        break
                                except Exception:
                                    continue

                        if not crawler_text or len(crawler_text) < 200:
                            selectors = [
                                "div.story-element-text",
                                ".story-element",
                                ".Iqx1L",
                                "article",
                                ".story-content",
                                ".article-body",
                                "#article-body",
                                ".post-content",
                                "main"
                            ]
                            for selector in selectors:
                                elements = crawler_soup.select(selector)
                                if elements:
                                    text_parts = [e.get_text(separator=" ", strip=True) for e in elements]
                                    full_text = "\n\n".join(text_parts)
                                    if len(full_text) > 200:
                                        crawler_text = full_text
                                        crawler_strategy = f"BS4: {selector}"
                                        break

                        if crawler_text and len(crawler_text) >= 50:
                            print("[NODE: RAW EXTRACTION] ✅ Crawler fallback succeeded.")
                            extracted_text = crawler_text
                            source_strategy = f"{crawler_strategy} (crawler-fallback)"
                            clean_html = crawler_clean_html
                            bulletproof_title = crawler_bulletproof_title
                            pub_date = crawler_pub_date
                            authors_str = crawler_authors
                            top_img = crawler_top_img
                            page_title = crawler_title
                        else:
                            print("[NODE: RAW EXTRACTION] ❌ Crawler fallback still returned empty content.")
                            page_title = crawler_title
                            html_content = crawler_html
                    else:
                        print("[NODE: RAW EXTRACTION] ❌ Crawler fallback could not retrieve page content.")

                finally:
                    await crawler_page.close()

            # --- 6. FINAL QUALITY CHECK ---
            if not extracted_text or len(extracted_text) < 50:
                if is_cdn_waf_challenge_page(page_title, html_content):
                    print("[NODE: RAW EXTRACTION] 🛑 CDN/WAF challenge page detected.")
                    return state.model_copy(update={
                        "error_message": f"Blocked by CDN/WAF challenge. Page Title: '{page_title}'"
                    })

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