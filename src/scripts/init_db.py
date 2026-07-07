import sys
import os
import argparse
from pymongo import MongoClient, ASCENDING
from datetime import datetime, timezone
import uuid

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.db.enums import PromptStatus
from src.configs.settings import settings

PROMPT_VERSION = "v1.0"

# --- PROMPT DEFINITIONS ---
INITIAL_DATA = [
    # --- 1. SUMMARY GENERATOR ---
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

    # --- 2. VALIDATION (CRITIC) ---
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

    # --- 3. LINK RELEVANCE CHECK ---
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

    # --- 4. SEARCH QUERY GENERATION ---
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

    # --- 5. CATEGORIZATION ---
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

    # --- 6. SEO GENERATION ---
    {
        "name": "seo_system",
        "content": """You are an expert SEO Meta Data Extractor.
Your task is to read news content and generate optimized metadata.

RULES:
1. Article Title: Create an original display headline, max 75 chars, that accurately reflects the article topic without copying the source title.
2. SEO Meta Title: Max 60 chars, distinct from the source title and article title when possible, no keyword stuffing.
3. SEO Meta Description: Max 160 chars, natural keyword placement.
4. Slug: Max 7-9 relevant words, lowercase, hyphen-separated.
5. Keywords: 3-5 strictly based on content.
6. Tone: Neutral, authoritative (BBC/Reuters style).
7. Avoid clickbait, hype, promotional wording, and unsupported claims.

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
{content_snippet}

Generate a unique article title suitable for our publication, plus SEO metadata. The article title must preserve the factual meaning of the source title while using original wording.""",
        "input_variables": ["title", "summary", "content_snippet"],
        "description": "User prompt for SEO metadata."
    },

    # --- 7. COUNTRY EXTRACTION ---
    {
        "name": "country_extraction_system",
        "content": """You are an expert in geographical entity extraction.
your task is to identify and extract the country or countries that the news article is primarily about.

RULES:
1. Extract only the countries that are central to the news story.
2. If the article mentions a city or state, extract the corresponding country.
3. If no specific country is relevant (e.g., general tech news), return an empty list.
4. Output must be a list of country names in English.
5. Do not include regions (like "Middle East") unless a specific country is not applicable.
6. Normalize country names (e.g., "UAE" -> "United Arab Emirates", "US" -> "United States").""",
        "input_variables": [],
        "description": "System instruction for country extraction."
    },
    {
        "name": "country_extraction_user",
        "content": """Please extract the relevant countries from the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---CONTENT SNIPPET---
{content}""",
        "input_variables": ["title", "summary", "content"],
        "description": "User prompt for country extraction."
    },

    # --- 8. CONTENT ENRICHMENT ---
    {
        "name": "content_enrichment_system",
        "content": """You are an impartial real estate market editor.
Your task is to write a single contextual paragraph for a section titled "Why this matters".

RULES:
1. Be thoughtful, neutral, and evidence-aware.
2. Use the article facts first, then the supplied contextual sources when useful.
3. Do not hype the story, make investment recommendations, or imply certainty beyond the evidence.
4. Do not invent statistics, forecasts, company claims, or market movements.
5. Distinguish broad context from confirmed article facts.
6. Do not include the section heading in the response.""",
        "input_variables": [],
        "description": "System instruction for the Why this matters content enrichment section."
    },
    {
        "name": "content_enrichment_user",
        "content": """Create the "Why this matters" paragraph for this article.

---TITLE---
{title}

---SUMMARY---
{summary}

---CATEGORIES---
{categories}

---COUNTRIES---
{countries}

---ARTICLE EXCERPT---
{article_excerpt}
---END ARTICLE EXCERPT---

---CONTEXTUAL SOURCES---
{contextual_sources}
---END CONTEXTUAL SOURCES---

REQUIREMENTS:
1. Write one paragraph only, 80-120 words.
2. Explain the broader relevance for readers interested in real estate, development, investment climate, regulation, infrastructure, or urban change.
3. Include a contextual reference from the provided sources when it is relevant.
4. If sources are weak or unavailable, stay limited to cautious implications from the article itself.
5. Avoid promotional language and avoid direct financial advice.""",
        "input_variables": [
            "title",
            "summary",
            "categories",
            "countries",
            "article_excerpt",
            "contextual_sources",
        ],
        "description": "User prompt for the Why this matters content enrichment section."
    },

    # --- 9. ARABIC TRANSLATION ---
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

    # --- 10. SOCIAL MEDIA CAPTION ---
    {
        "name": "social_caption_system",
        "content": """You are a world-class Direct Response Copywriter and Social Media Strategist.
Your goal is to drive high click-through rates (CTR) and App Downloads.

TONE:
- Urgent, engaging, and professional but accessible.
- Use psychological triggers (FOMO, curiosity, value).
- Avoid passive voice. Be punchy.

OBJECTIVE:
- Summarize the news hook instantly.
- Make the reader feel they must read the full story or use the app to stay ahead.
- The Call to Action (CTA) must be strong and directive (e.g., "Download now", "Read full report").""",
        "input_variables": [],
        "description": "System instruction for social media copywriter."
    },
    {
        "name": "social_caption_user",
        "content": """Create a high-conversion social media post for this article:

TITLE: {title}
SUMMARY: {summary}
READING TIME: {reading_time} mins

REQUIREMENTS:
1. HEADLINE: A scroll-stopping hook (max 10 words).
2. BODY: Max one paragraph (2-3 sentences) explaining why this matters.
3. CTA: Direct users to download the Propt App for the full analysis.
4. HASHTAGS: Mix of broad and niche real estate/business tags.""",
        "input_variables": ["title", "summary", "reading_time"],
        "description": "User prompt for generating social media captions."
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

INITIAL_RECIPIENTS = [
    {"email": "khizer.saleem@11prop.com", "name": "Khizer Saleem Malik"},
    {"email": "hammad@11prop.com", "name": "Syed Hammad Shah"},
    {"email": "anfal@11prop.com", "name": "Anfal Gul"},
    {"email": "hassaan@11prop.com", "name": "Hassaan Sajid"},
    {"email": "maliha.khan@11prop.com", "name": "Maliha Khan"},
]


def _application_collection_names():
    return list(dict.fromkeys([
        "prompts",
        "email_recipients",
        "categories",
        "sources",
        "processed_articles",
        "archived_articles",
        "deleted_articles",
        settings.MONGO_DB_COLLECTION,
        settings.MONGO_DB_BIO_COLLECTION,
        "checkpoints",
    ]))


def reset_application_collections(db):
    print("--- Fresh mode: dropping NewsAgent application collections ---")
    for collection_name in _application_collection_names():
        db.drop_collection(collection_name)
        print(f"  [-] Dropped collection: {collection_name}")


def seed_prompts(db):
    print("--- Setting up Prompts ---")
    prompts_col = db["prompts"]
    prompts_col.create_index([("name", ASCENDING)], unique=False)

    active_status = PromptStatus.ACTIVE.value
    for data in INITIAL_DATA:
        now = datetime.now(timezone.utc)
        result = prompts_col.update_one(
            {"name": data["name"], "status": active_status},
            {
                "$set": {
                    "name": data["name"],
                    "content": data["content"],
                    "description": data["description"],
                    "input_variables": data["input_variables"],
                    "version": PROMPT_VERSION,
                    "status": active_status,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": str(uuid.uuid4()),
                    "created_at": now,
                },
            },
            upsert=True,
        )

        if result.upserted_id:
            print(f"  [+] Added Active Prompt: {data['name']}")
        else:
            print(f"  [>] Upserted Active Prompt: {data['name']}")


def seed_email_recipients(db):
    print("--- Setting up Email Recipients ---")
    recipients_col = db["email_recipients"]
    recipients_col.create_index([("email", ASCENDING)], unique=True)

    for recipient_data in INITIAL_RECIPIENTS:
        now = datetime.now(timezone.utc)
        result = recipients_col.update_one(
            {"email": recipient_data["email"]},
            {
                "$set": {"name": recipient_data["name"]},
                "$setOnInsert": {
                    "_id": str(uuid.uuid4()),
                    "email": recipient_data["email"],
                    "is_active": True,
                    "created_at": now,
                },
            },
            upsert=True,
        )

        if result.upserted_id:
            print(f"  [+] Added Recipient: {recipient_data['email']}")
        else:
            print(f"  [>] Upserted Recipient: {recipient_data['email']}")


def seed_categories(db):
    print("--- Setting up Categories ---")
    categories_col = db["categories"]
    categories_col.create_index([("name", ASCENDING)], unique=True)
    categories_col.create_index([("external_id", ASCENDING)])

    for cat_data in INITIAL_CATEGORIES:
        now = datetime.now(timezone.utc)
        result = categories_col.update_one(
            {"name": cat_data["name"]},
            {
                "$set": {
                    "name": cat_data["name"],
                    "external_id": cat_data.get("external_id"),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": str(uuid.uuid4()),
                    "created_at": now,
                },
            },
            upsert=True,
        )

        if result.upserted_id:
            print(f"  [+] Added Category: {cat_data['name']}")
        else:
            print(f"  [>] Upserted Category: {cat_data['name']}")


def ensure_operational_indexes(db):
    print("--- Setting up Scheduler Sources ---")
    sources_col = db["sources"]
    sources_col.create_index([("is_active", ASCENDING)])
    sources_col.create_index([("listing_url", ASCENDING)], unique=True)
    print("  [+] Sources collection ready")

    print("--- Setting up Processed Articles ---")
    articles_col = db["processed_articles"]
    articles_col.create_index([("url", ASCENDING)], unique=True)
    articles_col.create_index([("discovered_at", -1)])
    articles_col.create_index([("status", ASCENDING)])
    print("  [+] Processed articles collection ready")

    print("--- Setting up Archive & Trash ---")
    archive_col = db["archived_articles"]
    archive_col.create_index([("url", ASCENDING)], unique=True)
    archive_col.create_index([("archived_at", -1)])
    archive_col.create_index([("source_id", ASCENDING)])

    deleted_col = db["deleted_articles"]
    deleted_col.create_index([("url", ASCENDING)], unique=True)
    deleted_col.create_index([("deleted_at", -1)])
    print("  [+] Archive and trash collections ready")

    print("--- Setting up Vector Support Collections ---")
    vector_col = db[settings.MONGO_DB_COLLECTION]
    vector_col.create_index([("source_id", ASCENDING)])
    vector_col.create_index([("created_at", -1)])

    bio_col = db[settings.MONGO_DB_BIO_COLLECTION]
    bio_col.create_index([("type", ASCENDING)], unique=True)
    print("  [+] Vector support collections ready")


def init_db(mode: str = "upsert", yes: bool = False):
    mode = mode.lower()
    if mode == "fresh" and not yes:
        raise SystemExit(
            "Fresh mode drops NewsAgent application collections, including "
            "article/archive/trash/vector data. Re-run with --yes to confirm."
        )

    print(f"--- Connecting to MongoDB at: {settings.DATABASE_URL} ---")
    print(f"--- Database: {settings.MONGO_DB_NAME} | Mode: {mode} ---")

    client = None
    try:
        client = MongoClient(settings.DATABASE_URL)
        client.admin.command("ping")
        db = client[settings.MONGO_DB_NAME]

        if mode == "fresh":
            reset_application_collections(db)

        seed_prompts(db)
        seed_email_recipients(db)
        seed_categories(db)
        ensure_operational_indexes(db)

        print("--- Initialization Complete ---")

    except Exception as e:
        print(f"[FATAL] Database initialization failed: {e}")
        raise
    finally:
        if client:
            client.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Initialize or refresh NewsAgent MongoDB collections."
    )
    parser.add_argument(
        "--mode",
        choices=["upsert", "fresh"],
        default="upsert",
        help=(
            "upsert safely updates seed prompts/categories/indexes in an existing DB; "
            "fresh drops NewsAgent application collections first."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required with --mode fresh to confirm destructive collection drops.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    init_db(mode=args.mode, yes=args.yes)
