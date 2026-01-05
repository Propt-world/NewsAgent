import asyncio
import re
from typing import Set
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from src.utils.browser import get_async_browser_context
from src.utils.governance import GovernanceGatekeeper

# --- FILTERS ---
AD_PATTERNS = [
    r"/ads/", r"/ad/", r"doubleclick", r"googlead", r"outbrain",
    r"taboola", r"click\?", r"campaign", r"sponsored", r"promotion"
]
DOMAIN_BLOCKLIST = [
    "doubleclick.net", "googleadservices.com", "googlesyndication.com",
    "facebook.com", "twitter.com", "linkedin.com", "instagram.com",
    "outbrain.com", "taboola.com"
]
TEXT_BLOCKLIST_PATTERNS = [
    r"^share$", r"^tweet$", r"^post$", r"share on.*"
]

async def fetch_listing_page(url: str) -> str:
    """
    Fetches the HTML of a listing page using Async Playwright.
    Includes logic to handle 'Infinite Scroll' pages.
    """

    # --- 0. GOVERNANCE CHECK ---
    gatekeeper = GovernanceGatekeeper()
    
    if not gatekeeper.can_fetch(url):
        print(f"[LINK DISCOVERY] 🛑 Blocked by robots.txt: {url}")
        return ""

    # Blocking call to wait for slot (DB-configured delay)
    gatekeeper.wait_for_slot(url)

    playwright = None
    browser = None
    
    try:
        # --- 1. LAUNCH ---
        # Start an async browser using the context manager
        async with get_async_browser_context() as (p, browser):
            page = await browser.new_page()
            
            # --- 2. NAVIGATE ---
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            
            # --- 3. SCROLL HACK ---
            # Many news sites (like CNN/Reuters) use lazy-loading.
            # We run a quick JS command to scroll to the bottom.
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                # Wait 2s for the new items to populate
                await page.wait_for_timeout(2000) 
            except Exception:
                pass # If scroll fails, we just take what's visible
            
            # --- 4. RETURN CONTENT ---
            content = await page.content()
            return content

    except Exception as e:
        print(f"[LINK DISCOVERY] Error fetching {url}: {e}")
        return ""
    # Finally block is removed as context manager handles cleanup

def extract_valid_urls(html: str, base_url: str, url_pattern: str = None) -> Set[str]:
    """
    Parses HTML, removes ads/noise, and returns clean absolute URLs.
    (Logic remains largely the same as your previous version).
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove clutter elements
    for tag in soup.select("header, footer, nav, .ad, .advertisement, .sponsored, aside"):
        tag.decompose()

    links = soup.find_all("a", href=True)
    valid_urls = set()
    base_domain = urlparse(base_url).netloc

    for link in links:
        href = link.get("href")
        text = link.get_text(strip=True)

        # Normalize to absolute URL
        full_url = urljoin(base_url, href)
        full_url = full_url.strip(" :\"',")
        parsed = urlparse(full_url)

        # Checks: Same Domain?
        if parsed.netloc != base_domain:
            continue
        # Checks: User Pattern?
        if url_pattern and url_pattern not in full_url:
            continue
        # Checks: Blocklists (Ads, Socials)
        if any(re.search(p, full_url, re.IGNORECASE) for p in AD_PATTERNS):
            continue
        if any(b in parsed.netloc for b in DOMAIN_BLOCKLIST):
            continue

        valid_urls.add(full_url)

    return valid_urls