import sys
import types


if "newspaper" not in sys.modules:
    newspaper_stub = types.ModuleType("newspaper")

    class _DummyArticle:  # pragma: no cover
        pass

    newspaper_stub.Article = _DummyArticle
    sys.modules["newspaper"] = newspaper_stub

if "lxml" not in sys.modules:
    lxml_stub = types.ModuleType("lxml")
    html_stub = types.ModuleType("lxml.html")
    html_stub.tostring = lambda *args, **kwargs: ""
    lxml_stub.html = html_stub
    sys.modules["lxml"] = lxml_stub
    sys.modules["lxml.html"] = html_stub

if "bs4" not in sys.modules:
    bs4_stub = types.ModuleType("bs4")

    class _DummyBeautifulSoup:  # pragma: no cover
        def __init__(self, *args, **kwargs):
            pass

    bs4_stub.BeautifulSoup = _DummyBeautifulSoup
    sys.modules["bs4"] = bs4_stub

from src.graph.nodes.raw_extraction import is_cdn_waf_challenge_page


def test_is_cdn_waf_challenge_page_detects_marker_in_title():
    assert is_cdn_waf_challenge_page(
        "ERROR: The request could not be satisfied",
        "<html><body>ok</body></html>",
    )


def test_is_cdn_waf_challenge_page_detects_marker_in_body():
    html = "<html><body>Verification required before accessing this resource.</body></html>"
    assert is_cdn_waf_challenge_page("Welcome", html)


def test_is_cdn_waf_challenge_page_returns_false_for_normal_content():
    html = "<html><body>This is a standard news article body with no challenge text.</body></html>"
    assert not is_cdn_waf_challenge_page("Normal Article", html)