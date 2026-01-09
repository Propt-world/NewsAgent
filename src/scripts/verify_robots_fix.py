import sys
import os
from unittest.mock import MagicMock

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

# Mocking all heavy dependencies
sys.modules["redis"] = MagicMock()
sys.modules["pymongo"] = MagicMock()
sys.modules["tavily"] = MagicMock()
sys.modules["opik"] = MagicMock()
sys.modules["opik.integrations.langchain"] = MagicMock()

# We need to mock settings too because it might try to load env or do other stuff
import src.configs.settings
src.configs.settings.settings = MagicMock()
src.configs.settings.settings.USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
src.configs.settings.settings.REDIS_URL = "redis://localhost:6379/0"
src.configs.settings.settings.DATABASE_URL = "mongodb://localhost:27017"
src.configs.settings.settings.MONGO_DB_NAME = "newsagent"

from src.utils.governance import GovernanceGatekeeper

def verify_fix():
    print("--- 🕵️ Verifying Robots.txt Fix (Mocked Environment) ---")
    
    url = "https://gulfnews.com/business/property"
    gatekeeper = GovernanceGatekeeper()
    
    # Mocking redis.get to return None (no cache)
    gatekeeper.redis.get.return_value = None
    
    print(f"Checking URL: {url}")
    is_allowed = gatekeeper.can_fetch(url)
    
    print(f"\nIs Allowed: {is_allowed}")
    
    if is_allowed:
        print("✅ SUCCESS: The URL is now allowed by the Gatekeeper.")
    else:
        print("❌ FAILURE: The URL is still blocked by the Gatekeeper.")

if __name__ == "__main__":
    try:
        verify_fix()
    except Exception as e:
        print(f"Error during verification: {e}")
        import traceback
        traceback.print_exc()
