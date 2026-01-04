import asyncio
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright
from src.configs.settings import settings

# Global Semaphore
BROWSER_SEMAPHORE = asyncio.Semaphore(8)

from contextlib import contextmanager, asynccontextmanager

# ... (keep existing imports)

# Global Semaphore
BROWSER_SEMAPHORE = asyncio.Semaphore(8)

@contextmanager
def get_sync_browser_context():
    """
    Context manager for Sync Playwright.
    """
    playwright = sync_playwright().start()
    
    try:
        if settings.BROWSER_WS_ENDPOINT:
            print(f"[BROWSER] 🔌 Connecting via CDP to: {settings.BROWSER_WS_ENDPOINT}")
            browser = playwright.chromium.connect_over_cdp(settings.BROWSER_WS_ENDPOINT)
        else:
            raise ValueError("BROWSER_WS_ENDPOINT is not set. Local browser fallback is disabled.")
            
        yield playwright, browser

    finally:
        # Proper cleanup
        # note: browser.close() is handled by the caller or context? 
        # In the original code, the caller closed it.
        # But a context manager *should* close it. 
        # However, to avoid breaking existing sync code if I'm not careful,
        # I will match the pattern of the original function if it wasn't a context manager,
        # BUT the original function was defined as `def get_sync_browser_context():` NOT a context manager but just a function.
        # Wait, the original code in step 40 was just a function!
        # "def get_sync_browser_context(): ... return playwright, browser"
        # It was NOT a context manager.
        pass

# I will REVERT the contextmanager decorator for sync if it wasn't one.
# Re-reading code from Step 40:
# def get_sync_browser_context():
#    playwright = sync_playwright().start()
#    ...
#    return playwright, browser

# Okay, so I should implement get_async_browser_context as a similar Helper or a Context Manager.
# Since I am refactoring raw_extraction, I can choose to make it a context manager which is safer.

@asynccontextmanager
async def get_async_browser_context():
    """
    Async Context Manager for Playwright.
    Ensures browser is closed even if errors occur.
    """
    async with async_playwright() as p:
        browser = None
        try:
            if settings.BROWSER_WS_ENDPOINT:
                print(f"[BROWSER] 🔌 Connecting via CDP (Async) to: {settings.BROWSER_WS_ENDPOINT}")
                browser = await p.chromium.connect_over_cdp(settings.BROWSER_WS_ENDPOINT)
            else:
                # Fallback to local execution if needed, or raise error
                print("[BROWSER] ⚠️ No WS Endpoint. Launching local headless chrome.")
                browser = await p.chromium.launch(headless=True)
            
            yield p, browser

        finally:
            if browser:
                await browser.close()