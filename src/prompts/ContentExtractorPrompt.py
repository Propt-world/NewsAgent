ContentExtractor = """
You are a content extractor that analyzes raw text content and extracts structured article information.

Extract the following information from the provided text:
- title: The main headline or title of the article
- content: The main body text content (cleaned and formatted)
- summary: A brief summary of the article (2-3 sentences)
- url: The source URL if mentioned
- published_date: The publication date in YYYY-MM-DD format if available
- author: The author's name if mentioned
- category: A list of relevant categories/topics for the article
- keywords: A list of 3-5 key terms that describe the article
- embedded_links: A list of relevant links found in the content with their titles

Text to analyze:
{raw_content}

Please respond with a JSON object matching this schema:
{schema}

Extract as much information as possible from the text. If information is not available, use null for that field.
"""

schema = {
  "title": "string or null",
  "content": "string or null",
  "summary": "string or null",
  "url": "string or null",
  "published_date": "string or null (YYYY-MM-DD format)",
  "author": "string or null",
  "category": ["string"],
  "keywords": ["string"],
  "embedded_links": [
    {
      "url": "string",
      "title": "string",
      "description": "string or null"
    }
  ]
}