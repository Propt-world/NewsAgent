import urllib.request

url = "https://gulfnews.com/robots.txt"
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

req = urllib.request.Request(url, headers={'User-Agent': user_agent})
try:
    with urllib.request.urlopen(req) as response:
        content = response.read().decode('utf-8')
        print("--- CONTENT START ---")
        print(content)
        print("--- CONTENT END ---")
except Exception as e:
    print(f"Error: {e}")
