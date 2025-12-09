import traceback
from pprint import pprint
from requests_html import HTMLSession, MaxRetries
from newspaper import Article, Config
from lxml.html import tostring
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel

def raw_extraction(state: MainWorkflowState) -> MainWorkflowState:
    """
    Fetches, renders JavaScript, and extracts clean article TEXT and HTML.

    - Uses `requests_html` to load the page and render client-side JavaScript.
    - Uses `newspaper4k` to parse the rendered HTML and find the *main*
      article text, title, authors, and publish date.
    - Populates the state with the clean text (for summarization),
      the clean HTML (for link extraction), and a partial 'news_article'.
    """

    url = state.source_url
    pprint(f"[NODE: RAW EXTRACTION] Fetching and rendering: {url}")

    # Initialize an HTML Session (this manages the headless browser)
    # FIX: Initialize HTML Session with Docker-compatible browser arguments
    session = HTMLSession(
        browser_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-setuid-sandbox",
            "--single-process"
        ]
    )
    # --- NEWSPAPER4K CONFIGURATION ---
    # You can configure newspaper4k if needed.
    # For now, the default config is fine.
    # config = Config()
    # config.browser_user_agent = '...'

    try:
        # 1. Get the page and render JavaScript
        response = session.get(
            url,
            timeout=30,  # Increased timeout for JS rendering
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                              " AppleWebKit/537.36 (KHTML, like Gecko)"
                              " Chrome/124.0.0.0 Safari/537.36"
            }
        )

        response.raise_for_status()

        # 2. Render JavaScript
        response.html.render(scrolldown=2, timeout=30, sleep=1)
        pprint(f"[NODE: RAW EXTRACTION] Page rendered successfully.")

        # 3. Use newspaper4k to parse the *rendered* HTML
        # Pass the config if you created one: article = Article(url, config=config)
        article = Article(url)
        article.download(input_html=response.html.html) # Pass the rendered HTML
        article.parse()

        # 4. Check if newspaper4k found content
        if not article.text:
            pprint(f"[NODE: RAW EXTRACTION] newspaper4k found no content for: {url}")
            return state.model_copy(update={
                "error_message": "Failed to extract main article content (newspaper4k found no text)."
            })

        # 5. Pre-populate the ArticleModel (no changes here)
        author_str = ", ".join(article.authors) if article.authors else None
        date_str = article.publish_date.isoformat() if article.publish_date else None

        initial_article = ArticleModel(
            title=article.title,
            content=article.text, # This is the *clean* text
            published_date=date_str,
            author=author_str
        )

        # --- CAPTURE IMAGE ---
        top_image_url = article.top_image

        initial_article = ArticleModel(
            title=article.title,
            content=article.text,
            published_date=date_str,
            author=author_str,
            top_image=top_image_url
        )

        # 6. Get the clean HTML snippet (no changes here)
        clean_html = ""
        if article.top_node is not None:
            clean_html = tostring(article.top_node, encoding='unicode')

        pprint(f"[NODE: RAW EXTRACTION] Successfully extracted: {article.title}")

        # 7. Return a copy of the state with the new data
        return state.model_copy(update={
            "cleaned_article_text": article.text,    # For summary & validation
            "cleaned_article_html": clean_html,      # For link extraction
            "news_article": initial_article          # The partial model
        })

    except MaxRetries:
        pprint(f"[NODE: RAW EXTRACTION] Max retries exceeded for: {url}")
        return state.model_copy(update={
            "error_message": f"Error rendering {url}: Max retries exceeded (likely a JS-heavy page)."
        })
    except Exception as e:
        pprint(f"[NODE: RAW EXTRACTION] Error fetching/parsing {url}: {e}")
        traceback.print_exc() # Print the full error stack trace
        return state.model_copy(update={
            "error_message": f"Error in raw_extraction: {e}"
        })
    finally:
        # FIX: Ensure session is closed to prevent zombie processes
        try:
            session.close()
        except:
            pass