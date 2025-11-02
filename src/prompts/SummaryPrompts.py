# Prompts for the summary generation node

SYSTEM_PROMPT = """
You are an expert news summarizer. Your goal is to create a concise,
accurate summary of a news article in less than 100 words.
You must not change the tone, add any information (hallucinate),
or alter the semantic meaning of the original text.
"""

# This template will be used for the first attempt
INITIAL_USER_PROMPT = """
Please summarize the following article:

---ARTICLE---
{article_text}
---END ARTICLE---
"""

# This template will be used if the validator fails and sends it back
RETRY_USER_PROMPT = """
Your previous summary was rejected. Please regenerate it to fix the issue.

FEEDBACK:
{feedback}

---ORIGINAL ARTICLE---
{article_text}
---END ARTICLE---
"""