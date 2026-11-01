"""Cookie / consent pop-ups are never taken as the article (zawya, 2026-10-29). The real extraction step runs on
stand-in pages: no browser, no network."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from src.graph.nodes import raw_extraction as rx
from src.models.MainWorkflowState import MainWorkflowState

URL = "https://www.zawya.com/en/business/uae-realty-market/dubai-property-resale-market-soars"

# Zawya's OneTrust pop-up, as it came out as the article body on dev
CONSENT = """
<h2>Manage Consent Preferences</h2>
<h3>Strictly Necessary Cookies</h3><p>Always Active</p>
<p>These cookies are necessary for the website to function and cannot be switched off in our systems. They are
usually only set in response to actions made by you which amount to a request for services, such as setting your
privacy preferences, logging in or filling in forms.</p>
<h3>Performance Cookies</h3>
<p>These cookies allow us to count visits and traffic sources so we can measure and improve the performance of our
site. They help us to know which pages are the most and least popular and see how visitors move around the site.</p>
<h3>Functional Cookies</h3>
<p>These cookies enable the website to provide enhanced functionality and personalisation. They may be set by us or
by third party providers whose services we have added to our pages.</p>
<h3>Targeting Cookies</h3>
<p>These cookies may be set through our site by our advertising partners to build a profile of your interests and
show you relevant adverts on other sites. You can opt out of these cookies at any time.</p>
"""

STORY = """
<p>Dubai's resale property market recorded its strongest quarter on record as buyers turned to ready homes, with
secondary transactions rising 38 percent year on year while off-plan sales slowed for a second month.</p>
<p>Brokers said end-users and investors were drawn by immediate rental income and lower risk, with villas in Dubai
Hills Estate and apartments in Jumeirah Village Circle seeing the fastest growth in resale prices.</p>
<p>Analysts expect the shift to continue into next year as developers stretch payment plans and handover dates on
new launches, giving completed homes an advantage for buyers who want to move in or rent out quickly.</p>
"""


def page(body: str, head: str = "") -> str:
    return (f"<html><head><title>Dubai resale market soars</title>"
            f"<meta property='og:title' content='Dubai property resale market soars as off-plan demand slows'>{head}"
            f"</head><body>{body}</body></html>")


class FakePage:
    def __init__(self, html):
        self.html = html

    async def route(self, pattern, handler):
        return None

    async def goto(self, url, **kwargs):
        return None

    async def title(self):
        return "Dubai resale market soars"

    async def evaluate(self, script):
        return None

    async def wait_for_timeout(self, ms):
        return None

    def is_closed(self):
        return False

    async def content(self):
        return self.html


@pytest.fixture
def run(monkeypatch):
    class Gatekeeper:
        async def can_fetch(self, url):
            return True

        async def wait_for_slot(self, url):
            return None

    monkeypatch.setattr(rx, "AsyncGovernanceGatekeeper", Gatekeeper)

    def extract(html):
        class Browser:
            async def new_page(self, **kwargs):
                return FakePage(html)

        @asynccontextmanager
        async def context():
            yield None, Browser()

        monkeypatch.setattr(rx, "get_async_browser_context", context)
        return asyncio.run(rx.raw_extraction(MainWorkflowState(source_url=URL)))
    return extract


def test_a_known_consent_pop_up_is_removed_and_the_story_is_read(run):
    state = run(page(f"<div id='onetrust-consent-sdk'>{CONSENT}</div><article>{STORY}</article>"))
    assert state.error_message is None
    assert "resale property market" in state.cleaned_article_text
    assert "cookies" not in state.cleaned_article_text.lower()
    assert state.news_article.title == "Dubai property resale market soars as off-plan demand slows"


def test_a_pop_up_newspaper4k_takes_for_the_article_falls_back_to_the_site_s_own_article_body(run):
    # An unknown pop-up container, longer than the story, so Newspaper4k picks it; the site's JSON-LD has the story
    ld = json.dumps({"@type": "NewsArticle", "articleBody": STORY.replace("<p>", "").replace("</p>", "\n")})
    state = run(page(f"<div class='modal-x'>{CONSENT * 3}</div><div class='teaser'>Read more</div>",
                     head=f"<script type='application/ld+json'>{ld}</script>"))
    assert state.error_message is None
    assert "resale property market" in state.cleaned_article_text
    assert "cookies" not in state.cleaned_article_text.lower()


def test_only_a_consent_notice_stops_the_article_with_a_clear_reason(run):
    # The site shows nothing but the pop-up until cookies are accepted
    state = run(page(f"<div class='modal-x'>{CONSENT}</div>"))
    assert state.error_message.startswith("The page returned a cookie/consent notice instead of the article")
    assert state.news_article is None


def test_an_article_that_mentions_cookies_in_passing_is_kept(run):
    passing = STORY + "<p>The developer's website asks visitors to accept cookies before browsing its listings.</p>"
    state = run(page(f"<article>{passing}</article>"))
    assert state.error_message is None and "accept cookies" in state.cleaned_article_text


def test_consent_check():
    from bs4 import BeautifulSoup

    assert rx.is_consent_text(BeautifulSoup(CONSENT, "lxml").get_text("\n"))
    assert not rx.is_consent_text(BeautifulSoup(STORY, "lxml").get_text("\n"))
    assert not rx.is_consent_text("")


def test_a_big_wrapper_named_cookie_is_not_removed():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(f"<div class='cookie-wrapper'>{STORY * 20}</div><div class='cookie-bar'>We use cookies.</div>",
                         "lxml")
    assert rx.remove_consent_elements(soup) == 1  # only the small bar
    assert "resale property market" in soup.get_text()
