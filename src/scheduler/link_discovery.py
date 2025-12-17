import re
import asyncio
from typing import Set, List
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from requests_html import AsyncHTMLSession
from pyppeteer.errors import NetworkError, PageError
import requests
from src.utils.browser import get_async_html_session

# --- BLOCKLIST CONFIGURATION ---

# 1. URL Patterns (Path/Query) that indicate ads/tracking
AD_PATTERNS = [
    r"/ads/", r"/ad/", r"doubleclick", r"googlead", r"outbrain",
    r"taboola", r"click\?", r"campaign", r"sponsored", r"promotion"
]

# 2. Domains to explicitly ignore (Ads, Social Media, Analytics)
DOMAIN_BLOCKLIST = [
    "doubleclick.net", "googleadservices.com", "googlesyndication.com",
    "adservice.google.com", "analytics.google.com", "facebook.com",
    "twitter.com", "linkedin.com", "instagram.com", "pinterest.com",
    "ad.doubleclick.net", "c.ad.doubleclick.net", "platform.twitter.com",
    "syndication.twitter.com", "adobedtm.com", "omtrdc.net", "outbrain.com",
    "taboola.com", "sharethrough.com", "adsrvr.org"
]

# 3. Link Text patterns to ignore (Buttons, Social Shares)
TEXT_BLOCKLIST_PATTERNS = [
    r"^share$", r"^tweet$", r"^post$", r"^facebook$", r"^twitter$",
    r"^linkedin$", r"^pinterest$", r"^advertisement$", r"^related:$",
    r"share on.*", r"share to.*"
]

async def fetch_listing_page(url: str, render_js: bool = True) -> str:
    """
    Fetches the HTML of a listing page.
    Uses requests-html to optionally render JavaScript (for lazy loaded links).
    """
    # FIX: Add these arguments to prevent crashes in Docker
    # --no-sandbox: Required for running as root in Docker
    # --disable-dev-shm-usage: Prevents crashing when /dev/shm is small (Docker default is 64MB)
    session = get_async_html_session()
    
    try:
        response = await session.get(url, timeout=30)

        if render_js:
            try:
                # Render with safeguards
                # scrolldown=12 is usually enough to trigger lazy load without overwhelming memory
                await response.html.arender(scrolldown=2, sleep=1, timeout=15)
            "--disable-setuid-sandbox",
            "--single-process"
        ]
    )
    try:
        response = await session.get(url, timeout=30)

        if render_js:
            try:
                # Render with safeguards
                # scrolldown=12 is usually enough to trigger lazy load without overwhelming memory
                await response.html.arender(scrolldown=2, sleep=1, timeout=15)
            except (NetworkError, PageError, asyncio.TimeoutError) as e:
                print(f"[LINK DISCOVERY] Render failed for {url} (Using static HTML): {e}")
                # We do NOT raise here. We just return the static HTML we already downloaded.
            except Exception as e:
                print(f"[LINK DISCOVERY] Unexpected render error: {e}")

        return response.html.html

    except requests.exceptions.ConnectionError as e:
        print(f"[LINK DISCOVERY] Connection Error for {url}: {e}")
        raise e  # Re-raise so scheduler can log/email this specific failure
    except Exception as e:
        raise e
    finally:
        try:
            await session.close()
        except:
            pass

def extract_valid_urls(html: str, base_url: str, url_pattern: str = None) -> Set[str]:
    """
    Parses HTML and returns a set of unique, valid, absolute URLs.
    Includes logic to filter out ads, social media, and irrelevant sections.
    """
    soup = BeautifulSoup(html, "lxml")

    # --- 1. REMOVE NOISE ---
    # Remove elements that typically contain ads or navigation clutter
    for tag in soup.select("header, footer, nav, .ad, .advertisement, .sponsored, aside"):
        tag.decompose()

    links = soup.find_all("a", href=True)

    valid_urls = set()
    base_domain = urlparse(base_url).netloc

    for link in links:
        href = link.get("href")
        text = link.get_text(strip=True) # Extract text for filtering

        # --- 2. NORMALIZE ---
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # --- 3. FILTERING ---

        # A. Domain Check (Must be same site)
        # Note: This implicitly filters external ads, but we keep the blocklist check for safety
        if parsed.netloc != base_domain:
            continue

        # B. Pattern Check (User defined)
        if url_pattern and url_pattern not in full_url:
            continue

        # C. Basic Protocol Check
        if "#" in href or "javascript:" in href or "mailto:" in href:
            continue

        # D. URL Pattern Blocklist (Ads/Trackers in URL)
        if any(re.search(pattern, full_url, re.IGNORECASE) for pattern in AD_PATTERNS):
            continue

        # E. Domain Blocklist (Explicit bad domains)
        if any(block in parsed.netloc for block in DOMAIN_BLOCKLIST):
            continue

        # F. Text Blocklist (Social share buttons, etc.)
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in TEXT_BLOCKLIST_PATTERNS):
            continue

        valid_urls.add(full_url)

    return valid_urls