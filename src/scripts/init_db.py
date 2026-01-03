import sys
import os
from pymongo import MongoClient, ASCENDING
from datetime import datetime, timezone
import uuid

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.db.enums import PromptStatus
from src.configs.settings import settings

# --- PROMPT DEFINITIONS ---
INITIAL_DATA = [
    # --- 1. CONTENT EXTRACTION ---
    {
        "name": "content_extractor",
        "content": """You are a content extractor that analyzes raw text content and extracts structured article information.

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

Extract as much information as possible from the text. If information is not available, use null for that field.""",
        "input_variables": ["raw_content", "schema"],
        "description": "Initial extraction of structured data from raw text."
    },

    # --- 2. SUMMARY GENERATOR ---
    {
        "name": "summary_system",
        "content": """You are an expert news summarizer. Your goal is to create a concise,
accurate summary of a news article in less than 100 words.
You must not change the tone, add any information (hallucinate),
or alter the semantic meaning of the original text.""",
        "input_variables": [],
        "description": "System instruction for the summarizer."
    },
    {
        "name": "summary_initial_user",
        "content": """Please summarize the following article:

---ARTICLE---
{article_text}
---END ARTICLE---""",
        "input_variables": ["article_text"],
        "description": "First attempt at summarizing the article."
    },
    {
        "name": "summary_retry_user",
        "content": """Your previous summary was rejected. Please regenerate it to fix the issue.

FEEDBACK:
{feedback}

---ORIGINAL ARTICLE---
{article_text}
---END ARTICLE---""",
        "input_variables": ["feedback", "article_text"],
        "description": "Retry prompt used when validation fails."
    },

    # --- 3. VALIDATION (CRITIC) ---
    {
        "name": "validation_system",
        "content": """You are an expert "Critic" and "Editor". Your task is to evaluate a generated
summary against its original article. You must be objective and strict.

You will score the summary on two metrics:
1.  **Semantic Score (0.0-10.0):** How semantically similar is the summary to the original?
2.  **Tone Score (0.0-10.0):** How well does the summary's tone match the original article?

You will then decide if the summary is valid:
-   **is_valid (boolean):** Set to 'true' ONLY if Semantic Score >= 8.0 AND Tone Score >= 7.0 AND zero hallucinations.
-   Otherwise, set to 'false'.

Finally, provide feedback. If 'is_valid' is 'false', provide actionable feedback.""",
        "input_variables": [],
        "description": "System instruction for the critic/validator."
    },
    {
        "name": "validation_user",
        "content": """Please evaluate the following summary against the original article.

---ORIGINAL ARTICLE---
{article_text}
---END ORIGINAL ARTICLE---

---GENERATED SUMMARY---
{summary_text}
---END GENERATED SUMMARY---""",
        "input_variables": ["article_text", "summary_text"],
        "description": "User prompt submitting the summary for validation."
    },

    # --- 4. LINK RELEVANCE CHECK ---
    {
        "name": "relevance_system",
        "content": """You are an expert "Relevance Analyzer". Your task is to evaluate how
relevant a linked webpage is to the summary of a main news article.

- 10.0: Direct source / essential context.
- 5.0: Tangentially related.
- 0.0: Irrelevant (ads, homepage, different topic).

Base your score on the provided content from the linked page and the context.""",
        "input_variables": [],
        "description": "System instruction for checking link relevance."
    },
    {
        "name": "relevance_user",
        "content": """Please analyze the relevance of the linked page.

--- MAIN ARTICLE SUMMARY ---
{summary}

--- CONTEXT (Text surrounding the link) ---
{link_context}

--- LINKED PAGE CONTENT (First 1500 characters) ---
{link_content}
--- END LINKED PAGE CONTENT ---""",
        "input_variables": ["summary", "link_context", "link_content"],
        "description": "User prompt for scoring a specific link."
    },

    # --- 5. SEARCH QUERY GENERATION ---
    {
        "name": "search_system",
        "content": """You are an expert search query generator. Your task is to analyze an
article's title, summary, and publication date to generate keywords and a
list of 3-5 diverse, high-quality search queries.

- Include queries using the main keywords.
- Include queries using the title.
- If the publication date is provided, *use it* to narrow the time-frame.
- Create diverse queries.""",
        "input_variables": [],
        "description": "System instruction for generating search queries."
    },
    {
        "name": "search_user",
        "content": """Please generate keywords and search queries for the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---PUBLICATION DATE---
{publish_date}""",
        "input_variables": ["title", "summary", "publish_date"],
        "description": "User prompt for search query generation."
    },

    # --- 6. CATEGORIZATION ---
    {
        "name": "categorization_system",
        "content": """You are an expert "Article Classifier" for a real estate news service.
Your job is to assign relevant categories to a news article based on its summary and content.

You MUST choose from the following predefined "Knowledge Base":

- **Off-Plan & New Launches** Pre-construction projects and newly announced developments.
- **Payment Plans & Offers** Flexible payment options and developer incentives.
- **High-Yield & ROI** Properties focused on rental returns and capital appreciation.
- **Golden Visa & Residency** Real estate linked to long-term visa eligibility.
- **Luxury Residences** Ultra-high-end homes and brand partnerships.
- **Affordable & Mid-Market** Budget-friendly housing options and starter homes.
- **Shared Ownership** Crowdfunding and share-based property investment.
- **Vocational Holiday Homes** Vacation rentals and tourism-focused properties.
- **Mega-Projects & Giga-Cities** Massive government and private master developments.
- **Villa, Apartments & Townhouses** Properties located on beaches, islands, or marinas.
- **Community Spotlights** Guides and reviews of specific neighborhoods/areas.
- **Commercial & Co-Working** Office spaces, retail, and flexible work environments.
- **Industrial & Logistics** Warehousing, free zones, and industrial real estate.
- **Future Forecast** Market data on property valuations and predictions.
- **Rental Market Watch** Updates on rental prices, laws, and tenant trends.
- **Construction Updates** Progress reports on major projects.
- **Legal & Regulatory** Government laws, taxes, and property rules.
- **Mortgage & Financing** Banking news, interest rates, and loan advice.
- **Sustainability & Green Living** Eco-friendly developments and energy-efficient homes.
- **PropTech & AI** Technology transforming the real estate sector.
- **Smart Homes & Automation** IoT and connected living technologies.
- **Wellness & Lifestyle** Amenities focused on health, parks, and recreation.
- **Developer News** Corporate updates from major property developers.
- **People** Profiles of CEOs, agents, and architects.
- **Events & Expos** Coverage of real estate exhibitions and conferences.
- **Architecture & Design Trends** News on design styles, facades, and architectural innovation.
- **Interiors & Luxury Fit-Out** Trends in interior finishing, renovation, and furniture.
- **Building Materials & Tech** Physical construction tech and material market updates.
- **Landscape & Outdoor Living** Design of outdoor spaces, parks, and green communities.
- **Urban Planning & Infrastructure** Public realm, transport, and city-level planning news.

--- RULES ---
1. Select 3-4 categories.
2. Do not make up new categories.""",
        "input_variables": [],
        "description": "System instruction containing the Knowledge Base for categorization."
    },
    {
        "name": "categorization_user",
        "content": """Please categorize the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---FIRST 500 CHARACTERS OF CONTENT---
{content_snippet}
---END CONTENT---""",
        "input_variables": ["title", "summary", "content_snippet"],
        "description": "User prompt for categorization."
    },

    # --- 7. SEO GENERATION ---
    {
        "name": "seo_system",
        "content": """You are an expert SEO Meta Data Extractor.
Your task is to read news content and generate optimized metadata.

RULES:
1. SEO Meta Title: Max 60 chars, click-enticing, no keyword stuffing.
2. SEO Meta Description: Max 160 chars, natural keyword placement.
3. Slug: Max 7-9 relevant words, lowercase, hyphen-separated.
4. Keywords: 3-5 strictly based on content.
5. Tone: Neutral, authoritative (BBC/Reuters style).

Do NOT invent information. Optimized for Search Engines.""",
        "input_variables": [],
        "description": "System instruction for SEO generation."
    },
    {
        "name": "seo_user",
        "content": """Please generate SEO metadata for the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---CONTENT SNIPPET---
{content_snippet}""",
        "input_variables": ["title", "summary", "content_snippet"],
        "description": "User prompt for SEO metadata."
    },

    # --- 8. ARABIC TRANSLATION ---
    {
        "name": "translation_system",
        "content": """You are a professional news translator fluent in English and Modern Standard Arabic (MSA).
Your task is to translate real estate news articles from English to Arabic.

RULES:
1. Maintain a professional, journalistic tone (similar to Al Arabiya / Asharq Business).
2. Translate specific real estate terminology accurately (e.g., "Off-plan", "Freehold", "ROI").
3. Do not summarize; provide a faithful, full translation of the content.
4. Ensure the Arabic text flows naturally and is grammatically correct.""",
        "input_variables": [],
        "description": "System instruction for Arabic translation."
    },
    {
        "name": "translation_user",
        "content": """Please translate the following article details into Arabic:

---TITLE---
{title}

---SUMMARY---
{summary}

---FULL CONTENT---
{content}""",
        "input_variables": ["title", "summary", "content"],
        "description": "User prompt for translating the full article."
    },
]

# --- CATEGORIES (WITH EXTERNAL IDS) ---
INITIAL_CATEGORIES = [
    {"name": "Architecture & Design Trends", "external_id": "0598752f-fe7b-46c4-adb2-d0a120ac7ba4"},
    {"name": "Interiors & Luxury Fit-Out", "external_id": "f8fd0676-b020-4c9a-b211-cc151c1e269b"},
    {"name": "Building Materials & Tech", "external_id": "fdfe14d5-66ee-4e8b-ac8d-e323abd1be3c"},
    {"name": "Landscape & Outdoor Living", "external_id": "aa7a276a-0a40-499f-a5de-c183b55c14f0"},
    {"name": "Urban Planning & Infrastructure", "external_id": "69688c2a-5c18-4a1a-a7b9-a509ccbc34fd"},
    {"name": "Golden Visa & Residency", "external_id": "21cf167c-505f-461e-8fe0-57270da0525c"},
    {"name": "Luxury Residences", "external_id": "59dd334f-bda8-48af-8b0d-52cd7aca6024"},
    {"name": "Shared Ownership", "external_id": "8ff6ee28-2853-4b53-a1bd-3cc55ac2eaf3"},
    {"name": "Vocational Holiday Homes", "external_id": "3d0f5c64-6cff-4831-b257-9b95eb426eac"},
    {"name": "Mega-Projects & Giga-Cities", "external_id": "e1f60a8e-a1c3-4475-a823-492c4155ffed"},
    {"name": "Villa, Apartments & Townhouses", "external_id": "edecd3a0-faf5-442c-aed1-44fbf2215a96"},
    {"name": "Community Spotlights", "external_id": "25b6b7b5-3b78-4c3e-9e63-a7fc24b7fa26"},
    {"name": "Commercial & Co-Working", "external_id": "87506e37-09c8-403e-be73-032a4609a696"},
    {"name": "Industrial & Logistics", "external_id": "81a6c938-32ee-4566-ab9d-8c6eddd77a25"},
    {"name": "Future Forecast", "external_id": "a7c37a51-b56e-4a72-bf77-5a7529db2f41"},
    {"name": "Rental Market Watch", "external_id": "1d3cbd90-e97a-432e-b56d-1cdd11855916"},
    {"name": "Construction Updates", "external_id": "669b1027-c955-4570-a9c8-59aa07a98902"},
    {"name": "Legal & Regulatory", "external_id": "b182c602-97a8-445e-a074-866d40cf8804"},
    {"name": "Mortgage & Financing", "external_id": "32a4d01d-05cb-47a6-904b-f71e12428e3d"},
    {"name": "Sustainability & Green Living", "external_id": "0a8c9d0d-29fb-4985-93f2-3c94e602dde3"},
    {"name": "PropTech & AI", "external_id": "56d6c5b1-6740-4919-abd2-0ba5efa0ffb2"},
    {"name": "Smart Homes & Automation", "external_id": "4fadd5c6-c08a-4249-b5d4-0ce23b50521a"},
    {"name": "Wellness & Lifestyle", "external_id": "52542d6d-e687-47ab-be1d-e9e910283737"},
    {"name": "Developer News", "external_id": "85081255-4ad1-4f9d-9034-8f57aa72b485"},
    {"name": "People", "external_id": "996365bf-b3bc-416c-9d6e-e5efd7144941"},
    {"name": "Events & Expos", "external_id": "48ff44af-13aa-4440-9920-7a954568c037"},
    {"name": "Off-Plan & New Launches", "external_id": "113607c7-5878-40b8-94af-52025926213e"},
    {"name": "Payment Plans & Offers", "external_id": "8c6fb4b5-37cf-43e1-ac90-75157bd34aea"},
    {"name": "High-Yield & ROI", "external_id": "30ea66e9-a7ec-4ed1-96b9-edd9fecd4b6a"},
    {"name": "Affordable & Mid-Market", "external_id": "3b8ff43b-9c24-4af8-ae73-885fa66c9edb"}
]


def init_db():
    print(f"--- Connecting to MongoDB at: {settings.DATABASE_URL} ---")

    try:
        client = MongoClient(settings.DATABASE_URL)
        db = client[settings.MONGO_DB_NAME]

        # ==========================================
        # 1. PROMPTS COLLECTION
        # ==========================================
        print("--- Setting up Prompts ---")
        prompts_col = db["prompts"]
        prompts_col.create_index([("name", ASCENDING)], unique=False)

        for data in INITIAL_DATA:
            existing = prompts_col.find_one({
                "name": data["name"],
                "status": PromptStatus.ACTIVE
            })
            if not existing:
                new_prompt = {
                    "_id": str(uuid.uuid4()),
                    "name": data["name"],
                    "content": data["content"],
                    "description": data["description"],
                    "input_variables": data["input_variables"],
                    "version": "v1.0",
                    "status": PromptStatus.ACTIVE,
                    "created_at": datetime.now(timezone.utc)
                }
                prompts_col.insert_one(new_prompt)
                print(f"  [+] Added Active Prompt: {data['name']}")
            else:
                print(f"  [~] Skipped (Already Active): {data['name']}")

        # ==========================================
        # 2. EMAIL RECIPIENTS COLLECTION
        # ==========================================
        print("--- Setting up Email Recipients ---")
        recipients_col = db["email_recipients"]
        recipients_col.create_index([("email", ASCENDING)], unique=True)

        # List of recipients to seed
        recipients_to_add = [
            {"email": "khizer.saleem@11prop.com", "name": "Khizer Saleem Malik"},
            {"email": "hammad@11prop.com", "name": "Syed Hammad Shah"},
            {"email": "anfal@11prop.com", "name": "Anfal Gul"},
            {"email": "hassaan@11prop.com", "name": "Hassaan Sajid"},
            {"email": "maliha.khan@11prop.com", "name": "Maliha Khan"}
        ]

        for recipient_data in recipients_to_add:
            email = recipient_data["email"]
            name = recipient_data["name"]

            if not recipients_col.find_one({"email": email}):
                recipients_col.insert_one({
                    "_id": str(uuid.uuid4()),
                    "email": email,
                    "name": name,
                    "is_active": True,
                    "created_at": datetime.now(timezone.utc)
                })
                print(f"  [+] Added Recipient: {email} ({name})")
            else:
                print(f"  [~] Skipped Recipient (Already Exists): {email}")

        # ==========================================
        # 3. CATEGORIES COLLECTION (UPDATED)
        # ==========================================
        print("--- Setting up Categories ---")
        categories_col = db["categories"]
        categories_col.create_index([("name", ASCENDING)], unique=True)
        # Add index for external_id for faster lookups
        categories_col.create_index([("external_id", ASCENDING)])

        for cat_data in INITIAL_CATEGORIES:
            existing = categories_col.find_one({"name": cat_data["name"]})
            
            if not existing:
                # Insert new category with ID
                new_cat = {
                    "_id": str(uuid.uuid4()),
                    "name": cat_data["name"],
                    "external_id": cat_data.get("external_id"), # Store Postgres ID
                    "created_at": datetime.now(timezone.utc)
                }
                categories_col.insert_one(new_cat)
                print(f"  [+] Added Category: {cat_data['name']}")
            
            elif existing.get("external_id") != cat_data.get("external_id"):
                # Update existing category if the external_id is missing or different
                categories_col.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"external_id": cat_data.get("external_id")}}
                )
                print(f"  [>] Updated ID for Category: {cat_data['name']}")
            
            else:
                print(f"  [~] Skipped Category (Unchanged): {cat_data['name']}")

        # ==========================================
        # 4. SCHEDULER: SOURCES COLLECTION
        # ==========================================
        print("--- Setting up Scheduler Sources ---")
        sources_col = db["sources"]
        # Index on 'is_active' because the scheduler queries specifically for active sources every loop
        sources_col.create_index([("is_active", ASCENDING)])
        # Index on 'listing_url' for uniqueness checks when adding sources
        sources_col.create_index([("listing_url", ASCENDING)], unique=True)
        print("  [+] Sources collection ready (empty - add sources via API)")

        # ==========================================
        # 5. SCHEDULER: PROCESSED ARTICLES
        # ==========================================
        print("--- Setting up Processed Articles ---")
        articles_col = db["processed_articles"]
        articles_col.create_index([("url", ASCENDING)], unique=True)
        articles_col.create_index([("discovered_at", -1)])
        articles_col.create_index([("status", ASCENDING)])
        
        # ==========================================
        # 6. ARCHIVE & TRASH COLLECTIONS (NEW)
        # ==========================================
        print("--- Setting up Archive & Trash ---")
        
        # A. Archived Articles
        archive_col = db["archived_articles"]
        # Ensure URLs are unique in archive too
        archive_col.create_index([("url", ASCENDING)], unique=True)
        # Index for sorting by when it was archived
        archive_col.create_index([("archived_at", -1)])
        # Useful if you want to search archives by source
        archive_col.create_index([("source_id", ASCENDING)])

        # B. Deleted Articles (Soft Delete)
        deleted_col = db["deleted_articles"]
        # Ensure URLs are unique in trash (prevents double-deleting issues)
        deleted_col.create_index([("url", ASCENDING)], unique=True)
        # Index for expiration policies (e.g., delete items older than 30 days)
        deleted_col.create_index([("deleted_at", -1)])

        print("--- Initialization Complete ---")
        client.close()

    except Exception as e:
        print(f"[FATAL] Database initialization failed: {e}")


if __name__ == "__main__":
    init_db()