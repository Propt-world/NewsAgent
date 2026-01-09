import requests
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse

url = "https://gulfnews.com/business/property"
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

domain = urlparse(url).netloc
robots_url = f"{urlparse(url).scheme}://{domain}/robots.txt"

print(f"Fetching {robots_url} with UA...")
headers = {'User-Agent': user_agent}
try:
    response = requests.get(robots_url, headers=headers, timeout=10)
    response.raise_for_status()
    robots_content = response.text
    print("Fetch successful.")
except Exception as e:
    print(f"Fetch failed: {e}")
    exit(1)

rp = RobotFileParser()
rp.parse(robots_content.splitlines())

is_allowed = rp.can_fetch(user_agent, url)
print(f"URL: {url}")
print(f"UA: {user_agent}")
print(f"Is Allowed: {is_allowed}")

# Test with a simpler UA
print(f"Is Allowed (Googlebot): {rp.can_fetch('Googlebot', url)}")
print(f"Is Allowed (*): {rp.can_fetch('*', url)}")
