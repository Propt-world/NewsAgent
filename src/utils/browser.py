from requests_html import HTMLSession, AsyncHTMLSession

# Common arguments for running Chromium in Docker
DOCKER_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-setuid-sandbox",
    "--single-process" 
]

def get_html_session() -> HTMLSession:
    """Returns a synchronous HTMLSession configured for Docker."""
    return HTMLSession(browser_args=DOCKER_BROWSER_ARGS)

def get_async_html_session() -> AsyncHTMLSession:
    """Returns an async HTMLSession configured for Docker."""
    return AsyncHTMLSession(browser_args=DOCKER_BROWSER_ARGS)