import secrets

def generate_keys():
    """
    Generates secure, url-safe keys for various components.
    Length of 32 bytes results in a ~43 character string.
    """
    webhook_secret = secrets.token_urlsafe(32)
    api_key = secrets.token_urlsafe(32)
    browserless_token = secrets.token_urlsafe(32)

    print("\n--- 🔑 GENERATED PRODUCTION KEYS ---")
    print(f"1. Webhook Secret:     {webhook_secret}")
    print(f"2. NewsAgent API Key:  {api_key}")
    print(f"3. Browserless Token:  {browserless_token}")
    print("---------------------------------------")
    print("ACTION: Copy these keys and add them to your .env file as:")
    print(f"WEBHOOK_SECRET={webhook_secret}")
    print(f"NEWSAGENT_API_KEY={api_key}")
    print(f"BROWSERLESS_TOKEN={browserless_token}")
    print("---------------------------------------\n")

if __name__ == "__main__":
    generate_keys()