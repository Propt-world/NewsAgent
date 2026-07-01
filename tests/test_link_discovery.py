from src.scheduler.link_discovery import extract_valid_urls


def test_extract_valid_urls_filters_noise_and_external_links():
    base_url = "https://example.com/news"
    html = """
    <html><body>
      <a href="/news">same listing</a>
      <a href="/article/1">good article</a>
      <a href="https://example.com/page/2">pagination</a>
      <a href="https://facebook.com/example">social</a>
      <a href="https://other.com/article/9">external</a>
      <a href="/ads/tracker">ad link</a>
    </body></html>
    """

    urls = extract_valid_urls(html, base_url)

    assert "https://example.com/article/1" in urls
    assert "https://example.com/news" not in urls
    assert "https://example.com/page/2" not in urls
    assert "https://facebook.com/example" not in urls
    assert "https://other.com/article/9" not in urls
    assert "https://example.com/ads/tracker" not in urls


def test_extract_valid_urls_applies_custom_url_pattern():
    base_url = "https://example.com/news"
    html = """
    <html><body>
      <a href="/business/a">business a</a>
      <a href="/sports/b">sports b</a>
    </body></html>
    """

    urls = extract_valid_urls(html, base_url, url_pattern="/business/")

    assert urls == {"https://example.com/business/a"}