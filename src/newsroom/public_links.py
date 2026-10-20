"""
The public link of a Propt Newsroom (composed) article, for showing to people only.

Composed articles have no publisher page; inside the system they are stored under an
internal key (newsroom://compose/<run>) and found by id. Wherever a link is shown
(chatbot citations, source references, signal evidence) they get the same link the
Propt app shares: https://propt.global/news/<slug>/<id>, the slug made from the
current title exactly like the app's slugify (src/screens/articleDetail/ArticleDetailScreen.tsx).
Nothing reads an article through this link.
"""
import re
from typing import Optional

PUBLIC_NEWS_BASE = "https://propt.global/news"
COMPOSED_SOURCE_ID = "newsroom_compose"


def slugify(text: str) -> str:
    """Same steps as the app (JavaScript \\w is ASCII-only, hence re.ASCII)."""
    s = str(text).lower().strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w-]+", "", s, flags=re.ASCII)
    s = re.sub(r"--+", "-", s)
    return s.strip("-")


def public_article_url(article_id: str, title: Optional[str]) -> str:
    return f"{PUBLIC_NEWS_BASE}/{slugify(title or 'article') or 'article'}/{article_id}"


def shown_url(doc: dict) -> Optional[str]:
    """The link to show for a stored article: the public link for composed ones, else its own url."""
    if (doc or {}).get("source_id") == COMPOSED_SOURCE_ID and doc.get("_id"):
        title = (doc.get("final_output") or {}).get("title")
        return public_article_url(str(doc["_id"]), title)
    return (doc or {}).get("url")
