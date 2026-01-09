import sys
import os

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

# Mocking tavily to avoid import errors
from unittest.mock import MagicMock
sys.modules["tavily"] = MagicMock()

from src.configs.settings import settings
from src.utils.governance import GovernanceGatekeeper

def test_user_agent_config():
    print("--- 🕵️ Testing User Agent Configuration ---")
    
    # 1. Check Settings
    ua = settings.USER_AGENT
    print(f"Status: Loaded from Settings")
    print(f"User-Agent: {ua}")
    
    if "Mozilla" not in ua:
        print("❌ WARNING: User Agent does not look like a browser!")
    else:
        print("✅ User Agent looks valid.")

    # 2. Check Governance Gatekeeper
    gatekeeper = GovernanceGatekeeper()
    gk_ua = gatekeeper.user_agent
    
    print(f"\nGatekeeper User-Agent: {gk_ua}")
    
    if gk_ua == ua:
        print("✅ GovernanceGatekeeper is using the configured User Agent.")
    else:
        print(f"❌ GovernanceGatekeeper mismatch! Expected {ua} but got {gk_ua}")

if __name__ == "__main__":
    test_user_agent_config()
