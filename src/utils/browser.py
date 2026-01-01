import asyncio
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright
from src.configs.settings import settings

# Global Semaphore
BROWSER_SEMAPHORE = asyncio.Semaphore(8)

def get_sync_browser_context():
    """
    Context manager for Sync Playwright.
    """
    playwright = sync_playwright().start()
    
    
    if settings.BROWSER_WS_ENDPOINT:
        print(f"[BROWSER] 🔌 Connecting via CDP to: {settings.BROWSER_WS_ENDPOINT}")
        # CHANGED: connect -> connect_over_cdp
        # This bypasses the strict version check
        browser = playwright.chromium.connect_over_cdp(settings.BROWSER_WS_ENDPOINT)
    else:
        raise ValueError("BROWSER_WS_ENDPOINT is not set. Local browser fallback is disabled.")
        
    return playwright, browser

async def launch_async_browser():
    """
    Async version.
    """
    p = await async_playwright().start()
    
    if settings.BROWSER_WS_ENDPOINT:
        print(f"[BROWSER] 🔌 Connecting via CDP (Async) to: {settings.BROWSER_WS_ENDPOINT}")
        # CHANGED: connect -> connect_over_cdp
        browser = await p.chromium.connect_over_cdp(settings.BROWSER_WS_ENDPOINT)
    else:
        raise ValueError("BROWSER_WS_ENDPOINT is not set. Local browser fallback is disabled.")
        
    return p, browser