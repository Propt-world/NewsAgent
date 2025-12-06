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
- category: The main category/topic of the article
- sub_category: A more specific subcategory if applicable
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
Your job is to assign relevant main categories and sub-categories
to a news article based on its summary and content.

You MUST choose from the following predefined "Knowledge Base":

- **Market & Economy**: GCC Market Overview, UAE Market, Saudi Market, Qatar Market, Oman Market, Bahrain Market, Kuwait Market, Real Estate Indices, Economic Trends & Data, Market Comparisons
- **Residential**: Apartments, Villas & Townhouses, Off-Plan Projects, Rental Market, Luxury Homes, Affordable Housing, Community Developments, Serviced Residences
- **Commercial**: Office Spaces, Retail Spaces, Warehousing & Industrial, Co-Working Hubs, Mixed-Use Projects, Logistics Parks, Free Zones Developments
- **Hospitality**: Hotel Developments, Resort Projects, Branded Residences, Serviced Apartments, Tourism Real Estate, Hospitality Investments
- **Development**: Mega Projects, Masterplans, Urban Planning, Infrastructure Projects, Mixed-Use Developments, Public Sector Developments
- **Developers**: UAE: Emaar, Aldar, Damac, Nakheel, Sobha, Binghatti, Meraas, Ellington, Danube, Azizi, Dubai Properties, KSA: Roshn, Red Sea Global, Dar Al Arkan, Jeddah Central Development, PIF Projects, Qatar: UDC, Barwa, Qatari Diar, Oman & Bahrain: Omran, Diyar Al Muharraq
- **Investment**: REITs, Institutional Investment, Capital Flows, Private Equity, Foreign Investment, Mortgage Trends, ROI Insights, Real Estate Funds
- **Finance**: Property Financing, Mortgage Rates, Valuations, Interest Rate Updates, Developer Payment Plans, Bank & Lender News
- **PropTech**: Smart Property Solutions, Real Estate Data & AI, Automation Tools, CRM & Software Platforms, Blockchain in Real Estate, Virtual Tours (AR/VR), Digital Twins, Online Marketplaces
- **Construction**: Contractors, Building Materials, Infrastructure Works, Engineering Firms, Project Management, Construction Updates, Safety Standards, Construction Technology
- **Architecture & Design**: Urban Architecture, Sustainable Building Design, Masterplanning, Façade Innovation, Architectural Firms, Landmark Projects
- **Sustainability**: Green Buildings, ESG in Real Estate, Smart Cities, Renewable Energy Integration, Carbon Neutral Projects, Environmental Standards, Sustainable Infrastructure
- **Policy & Regulations**: Ownership Laws, Freehold & Leasehold Rules, Golden Visa Regulations, Property Taxation, RERA & DLD Policies, Zoning & Development Laws, Government Real Estate Initiatives
- **People**: Developers & CEOs, Government Officials, Real Estate Analysts, Architects & Planners, Top Brokers, Industry Thought Leaders

--- RULES ---
1. Select 1-3 main categories.
2. Select any number of relevant sub-categories.
3. If no sub-categories fit, return empty list.
4. Do not make up new categories.""",
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

# --- CATEGORIES (NEW) ---
INITIAL_CATEGORIES = [
    {"name": "Market & Economy", "sub_categories": ["GCC Market Overview", "UAE Market", "Saudi Market", "Qatar Market", "Oman Market", "Bahrain Market", "Kuwait Market", "Real Estate Indices", "Economic Trends & Data", "Market Comparisons"]},
    {"name": "Residential", "sub_categories": ["Apartments", "Villas & Townhouses", "Off-Plan Projects", "Rental Market", "Luxury Homes", "Affordable Housing", "Community Developments", "Serviced Residences"]},
    {"name": "Commercial", "sub_categories": ["Office Spaces", "Retail Spaces", "Warehousing & Industrial", "Co-Working Hubs", "Mixed-Use Projects", "Logistics Parks", "Free Zones Developments"]},
    {"name": "Hospitality", "sub_categories": ["Hotel Developments", "Resort Projects", "Branded Residences", "Serviced Apartments", "Tourism Real Estate", "Hospitality Investments"]},
    {"name": "Development", "sub_categories": ["Mega Projects", "Masterplans", "Urban Planning", "Infrastructure Projects", "Mixed-Use Developments", "Public Sector Developments"]},
    {"name": "Developers", "sub_categories": ["UAE: Emaar", "Aldar", "Damac", "Nakheel", "Sobha", "Binghatti", "Meraas", "Ellington", "Danube", "Azizi", "Dubai Properties", "KSA: Roshn", "Red Sea Global", "Dar Al Arkan", "Jeddah Central Development", "PIF Projects", "Qatar: UDC", "Barwa", "Qatari Diar", "Oman & Bahrain: Omran", "Diyar Al Muharraq"]},
    {"name": "Investment", "sub_categories": ["REITs", "Institutional Investment", "Capital Flows", "Private Equity", "Foreign Investment", "Mortgage Trends", "ROI Insights", "Real Estate Funds"]},
    {"name": "Finance", "sub_categories": ["Property Financing", "Mortgage Rates", "Valuations", "Interest Rate Updates", "Developer Payment Plans", "Bank & Lender News"]},
    {"name": "PropTech", "sub_categories": ["Smart Property Solutions", "Real Estate Data & AI", "Automation Tools", "CRM & Software Platforms", "Blockchain in Real Estate", "Virtual Tours (AR/VR)", "Digital Twins", "Online Marketplaces"]},
    {"name": "Construction", "sub_categories": ["Contractors", "Building Materials", "Infrastructure Works", "Engineering Firms", "Project Management", "Construction Updates", "Safety Standards", "Construction Technology"]},
    {"name": "Architecture & Design", "sub_categories": ["Urban Architecture", "Sustainable Building Design", "Masterplanning", "Façade Innovation", "Architectural Firms", "Landmark Projects"]},
    {"name": "Sustainability", "sub_categories": ["Green Buildings", "ESG in Real Estate", "Smart Cities", "Renewable Energy Integration", "Carbon Neutral Projects", "Environmental Standards", "Sustainable Infrastructure"]},
    {"name": "Policy & Regulations", "sub_categories": ["Ownership Laws", "Freehold & Leasehold Rules", "Golden Visa Regulations", "Property Taxation", "RERA & DLD Policies", "Zoning & Development Laws", "Government Real Estate Initiatives"]},
    {"name": "People", "sub_categories": ["Developers & CEOs", "Government Officials", "Real Estate Analysts", "Architects & Planners", "Top Brokers", "Industry Thought Leaders"]}
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
            {"email": "admin@example.com", "name": "System Admin"},
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
        # 3. CATEGORIES COLLECTION (NEW)
        # ==========================================
        print("--- Setting up Categories ---")
        categories_col = db["categories"]
        categories_col.create_index([("name", ASCENDING)], unique=True)

        for cat_data in INITIAL_CATEGORIES:
            existing = categories_col.find_one({"name": cat_data["name"]})
            if not existing:
                new_cat = {
                    "_id": str(uuid.uuid4()),
                    "name": cat_data["name"],
                    "sub_categories": cat_data["sub_categories"],
                    "created_at": datetime.now(timezone.utc)
                }
                categories_col.insert_one(new_cat)
                print(f"  [+] Added Category: {cat_data['name']}")
            else:
                print(f"  [~] Skipped Category: {cat_data['name']}")

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
        print("--- Setting up Processed Articles Archive ---")
        articles_col = db["processed_articles"]

        # CRITICAL PERFORMANCE INDEX
        # This ensures that `find({"url": ...})` is instant, even with millions of records.
        # unique=True also acts as a database-level constraint against duplicates.
        articles_col.create_index([("url", ASCENDING)], unique=True)

        # Index for the Dashboard (to sort by date efficiently)
        articles_col.create_index([("discovered_at", -1)])

        # Index for Status Filtering
        articles_col.create_index([("status", ASCENDING)])

        print("--- Initialization Complete ---")
        client.close()

    except Exception as e:
        print(f"[FATAL] Database initialization failed: {e}")


if __name__ == "__main__":
    init_db()
