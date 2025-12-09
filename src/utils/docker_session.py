import pyppeteer
from requests_html import HTMLSession, AsyncHTMLSession

# We define the path explicitly since the library ignores the ENV var
SYSTEM_CHROMIUM_PATH = '/usr/bin/chromium'

class DockerHTMLSession(HTMLSession):
    """
    A custom HTMLSession that forces requests-html to use the
    system-installed Chromium instead of trying to download one.
    """
    @property
    async def browser(self):
        if not hasattr(self, "_browser"):
            # We access the parent's browser_args (name mangled because it's private)
            args = getattr(self, '_BaseSession__browser_args', ['--no-sandbox'])

            # Explicitly pass executablePath to pyppeteer
            self._browser = await pyppeteer.launch(
                ignoreHTTPSErrors=not(self.verify),
                headless=True,
                args=args,
                executablePath=SYSTEM_CHROMIUM_PATH  # <--- THE CRITICAL FIX
            )
        return self._browser

class DockerAsyncHTMLSession(AsyncHTMLSession):
    """
    Async version of the DockerHTMLSession.
    """
    @property
    async def browser(self):
        if not hasattr(self, "_browser"):
            args = getattr(self, '_BaseSession__browser_args', ['--no-sandbox'])

            self._browser = await pyppeteer.launch(
                ignoreHTTPSErrors=not(self.verify),
                headless=True,
                args=args,
                executablePath=SYSTEM_CHROMIUM_PATH # <--- THE CRITICAL FIX
            )
        return self._browser