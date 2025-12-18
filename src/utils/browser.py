import asyncio
from playwright.async_api import async_playwright, Browser as AsyncBrowser
from playwright.sync_api import sync_playwright, Browser as SyncBrowser

# Global Semaphore to limit concurrent browser contexts across the entire worker
# On t3.xlarge (16GB), 8 concurrent tabs is a safe, high-performance limit.
BROWSER_SEMAPHORE = asyncio.Semaphore(8)

def get_sync_browser_context():
    """
    Context manager for a Sync Playwright browser.
    Used by synchronous nodes like raw_extraction.
    """
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    return playwright, browser

async def launch_async_browser():
    """
    Helper to launch an async browser.
    Used by async nodes like link_discovery.
    """
    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    return p, browser