from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse

url = "https://gulfnews.com/business/property"
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

domain = urlparse(url).netloc
robots_url = f"{urlparse(url).scheme}://{domain}/robots.txt"

rp = RobotFileParser()
rp.set_url(robots_url)
rp.read()

is_allowed = rp.can_fetch(user_agent, url)
print(f"URL: {url}")
print(f"UA: {user_agent}")
print(f"Is Allowed: {is_allowed}")
