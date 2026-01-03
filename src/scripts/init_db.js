// init_db.js
// Run this with: mongosh "YOUR_CONNECTION_STRING" init_db.js

print("--- ⚠️ STARTING DATABASE RESET ⚠️ ---");
print("--- This will DROP all collections and recreate them. ---");

// Helper to generate a UUID string (similar to Python's uuid.uuid4())
function uuidv4() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        var r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}

// ==========================================
// 1. DATA DEFINITIONS
// ==========================================

const INITIAL_PROMPTS = [
    {
        name: "content_extractor",
        status: "active",
        version: "v1.0",
        description: "Initial extraction of structured data from raw text.",
        input_variables: ["raw_content", "schema"],
        content: `You are a content extractor that analyzes raw text content and extracts structured article information.

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

Extract as much information as possible from the text. If information is not available, use null for that field.`
    },
    {
        name: "summary_system",
        status: "active",
        version: "v1.0",
        description: "System instruction for the summarizer.",
        input_variables: [],
        content: `You are an expert news summarizer. Your goal is to create a concise,
accurate summary of a news article in less than 100 words.
You must not change the tone, add any information (hallucinate),
or alter the semantic meaning of the original text.`
    },
    {
        name: "summary_initial_user",
        status: "active",
        version: "v1.0",
        description: "First attempt at summarizing the article.",
        input_variables: ["article_text"],
        content: `Please summarize the following article:

---ARTICLE---
{article_text}
---END ARTICLE---`
    },
    {
        name: "summary_retry_user",
        status: "active",
        version: "v1.0",
        description: "Retry prompt used when validation fails.",
        input_variables: ["feedback", "article_text"],
        content: `Your previous summary was rejected. Please regenerate it to fix the issue.

FEEDBACK:
{feedback}

---ORIGINAL ARTICLE---
{article_text}
---END ARTICLE---`
    },
    {
        name: "validation_system",
        status: "active",
        version: "v1.0",
        description: "System instruction for the critic/validator.",
        input_variables: [],
        content: `You are an expert "Critic" and "Editor". Your task is to evaluate a generated
summary against its original article. You must be objective and strict.

You will score the summary on two metrics:
1.  **Semantic Score (0.0-10.0):** How semantically similar is the summary to the original?
2.  **Tone Score (0.0-10.0):** How well does the summary's tone match the original article?

You will then decide if the summary is valid:
-   **is_valid (boolean):** Set to 'true' ONLY if Semantic Score >= 8.0 AND Tone Score >= 7.0 AND zero hallucinations.
-   Otherwise, set to 'false'.

Finally, provide feedback. If 'is_valid' is 'false', provide actionable feedback.`
    },
    {
        name: "validation_user",
        status: "active",
        version: "v1.0",
        description: "User prompt submitting the summary for validation.",
        input_variables: ["article_text", "summary_text"],
        content: `Please evaluate the following summary against the original article.

---ORIGINAL ARTICLE---
{article_text}
---END ORIGINAL ARTICLE---

---GENERATED SUMMARY---
{summary_text}
---END GENERATED SUMMARY---`
    },
    {
        name: "relevance_system",
        status: "active",
        version: "v1.0",
        description: "System instruction for checking link relevance.",
        input_variables: [],
        content: `You are an expert "Relevance Analyzer". Your task is to evaluate how
relevant a linked webpage is to the summary of a main news article.

- 10.0: Direct source / essential context.
- 5.0: Tangentially related.
- 0.0: Irrelevant (ads, homepage, different topic).

Base your score on the provided content from the linked page and the context.`
    },
    {
        name: "relevance_user",
        status: "active",
        version: "v1.0",
        description: "User prompt for scoring a specific link.",
        input_variables: ["summary", "link_context", "link_content"],
        content: `Please analyze the relevance of the linked page.

--- MAIN ARTICLE SUMMARY ---
{summary}

--- CONTEXT (Text surrounding the link) ---
{link_context}

--- LINKED PAGE CONTENT (First 1500 characters) ---
{link_content}
--- END LINKED PAGE CONTENT ---`
    },
    {
        name: "search_system",
        status: "active",
        version: "v1.0",
        description: "System instruction for generating search queries.",
        input_variables: [],
        content: `You are an expert search query generator. Your task is to analyze an
article's title, summary, and publication date to generate keywords and a
list of 3-5 diverse, high-quality search queries.

- Include queries using the main keywords.
- Include queries using the title.
- If the publication date is provided, *use it* to narrow the time-frame.
- Create diverse queries.`
    },
    {
        name: "search_user",
        status: "active",
        version: "v1.0",
        description: "User prompt for search query generation.",
        input_variables: ["title", "summary", "publish_date"],
        content: `Please generate keywords and search queries for the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---PUBLICATION DATE---
{publish_date}`
    },
    {
        name: "categorization_system",
        status: "active",
        version: "v1.0",
        description: "System instruction containing the Knowledge Base for categorization.",
        input_variables: [],
        content: `You are an expert "Article Classifier" for a real estate news service.
Your job is to assign relevant categories to a news article based on its summary and content.

You MUST choose from the following predefined "Knowledge Base":

- **Off-Plan & New Launches**	Pre-construction projects and newly announced developments.
- **Payment Plans & Offers**	Flexible payment options and developer incentives.
- **High-Yield & ROI**	Properties focused on rental returns and capital appreciation.
- **Golden Visa & Residency**	Real estate linked to long-term visa eligibility.
- **Luxury Residences**	Ultra-high-end homes and brand partnerships.
- **Affordable & Mid-Market**	Budget-friendly housing options and starter homes.
- **Shared Ownership**	Crowdfunding and share-based property investment.
- **Vocational Holiday Homes**	Vacation rentals and tourism-focused properties.
- **Mega-Projects & Giga-Cities**	Massive government and private master developments.
- **Villa, Apartments & Townhouses**	Properties located on beaches, islands, or marinas.
- **Community Spotlights**	Guides and reviews of specific neighborhoods/areas.
- **Commercial & Co-Working**	Office spaces, retail, and flexible work environments.
- **Industrial & Logistics**	Warehousing, free zones, and industrial real estate.
- **Future Forecast**	Market data on property valuations and predictions.
- **Rental Market Watch**	Updates on rental prices, laws, and tenant trends.
- **Construction Updates**	Progress reports on major projects.
- **Legal & Regulatory**	Government laws, taxes, and property rules.
- **Mortgage & Financing**	Banking news, interest rates, and loan advice.
- **Sustainability & Green Living**	Eco-friendly developments and energy-efficient homes.
- **PropTech & AI**	Technology transforming the real estate sector.
- **Smart Homes & Automation**	IoT and connected living technologies.
- **Wellness & Lifestyle**	Amenities focused on health, parks, and recreation.
- **Developer News**	Corporate updates from major property developers.
- **People**	Profiles of CEOs, agents, and architects.
- **Events & Expos**	Coverage of real estate exhibitions and conferences.
- **Architecture & Design Trends**	News on design styles, facades, and architectural innovation.
- **Interiors & Luxury Fit-Out**	Trends in interior finishing, renovation, and furniture.
- **Building Materials & Tech**	Physical construction tech and material market updates.
- **Landscape & Outdoor Living**	Design of outdoor spaces, parks, and green communities.
- **Urban Planning & Infrastructure**	Public realm, transport, and city-level planning news.

--- RULES ---
1. Select 3-4 categories.
2. Do not make up new categories.`
    },
    {
        name: "categorization_user",
        status: "active",
        version: "v1.0",
        description: "User prompt for categorization.",
        input_variables: ["title", "summary", "content_snippet"],
        content: `Please categorize the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---FIRST 500 CHARACTERS OF CONTENT---
{content_snippet}
---END CONTENT---`
    },
    {
        name: "seo_system",
        status: "active",
        version: "v1.0",
        description: "System instruction for SEO generation.",
        input_variables: [],
        content: `You are an expert SEO Meta Data Extractor.
Your task is to read news content and generate optimized metadata.

RULES:
1. SEO Meta Title: Max 60 chars, click-enticing, no keyword stuffing.
2. SEO Meta Description: Max 160 chars, natural keyword placement.
3. Slug: Max 7-9 relevant words, lowercase, hyphen-separated.
4. Keywords: 3-5 strictly based on content.
5. Tone: Neutral, authoritative (BBC/Reuters style).

Do NOT invent information. Optimized for Search Engines.`
    },
    {
        name: "seo_user",
        status: "active",
        version: "v1.0",
        description: "User prompt for SEO metadata.",
        input_variables: ["title", "summary", "content_snippet"],
        content: `Please generate SEO metadata for the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---CONTENT SNIPPET---
{content_snippet}`
    },
    {
        name: "translation_system",
        status: "active",
        version: "v1.0",
        description: "System instruction for Arabic translation.",
        input_variables: [],
        content: `You are a professional news translator fluent in English and Modern Standard Arabic (MSA).
Your task is to translate real estate news articles from English to Arabic.

RULES:
1. Maintain a professional, journalistic tone (similar to Al Arabiya / Asharq Business).
2. Translate specific real estate terminology accurately (e.g., "Off-plan", "Freehold", "ROI").
3. Do not summarize; provide a faithful, full translation of the content.
4. Ensure the Arabic text flows naturally and is grammatically correct.`
    },
    {
        name: "translation_user",
        status: "active",
        version: "v1.0",
        description: "User prompt for translating the full article.",
        input_variables: ["title", "summary", "content"],
        content: `Please translate the following article details into Arabic:

---TITLE---
{title}

---SUMMARY---
{summary}

---FULL CONTENT---
{content}`
    }
];

const INITIAL_CATEGORIES = [
    { name: "Architecture & Design Trends", external_id: "0598752f-fe7b-46c4-adb2-d0a120ac7ba4" },
    { name: "Interiors & Luxury Fit-Out", external_id: "f8fd0676-b020-4c9a-b211-cc151c1e269b" },
    { name: "Building Materials & Tech", external_id: "fdfe14d5-66ee-4e8b-ac8d-e323abd1be3c" },
    { name: "Landscape & Outdoor Living", external_id: "aa7a276a-0a40-499f-a5de-c183b55c14f0" },
    { name: "Urban Planning & Infrastructure", external_id: "69688c2a-5c18-4a1a-a7b9-a509ccbc34fd" },
    { name: "Golden Visa & Residency", external_id: "21cf167c-505f-461e-8fe0-57270da0525c" },
    { name: "Luxury Residences", external_id: "59dd334f-bda8-48af-8b0d-52cd7aca6024" },
    { name: "Shared Ownership", external_id: "8ff6ee28-2853-4b53-a1bd-3cc55ac2eaf3" },
    { name: "Vocational Holiday Homes", external_id: "3d0f5c64-6cff-4831-b257-9b95eb426eac" },
    { name: "Mega-Projects & Giga-Cities", external_id: "e1f60a8e-a1c3-4475-a823-492c4155ffed" },
    { name: "Villa, Apartments & Townhouses", external_id: "edecd3a0-faf5-442c-aed1-44fbf2215a96" },
    { name: "Community Spotlights", external_id: "25b6b7b5-3b78-4c3e-9e63-a7fc24b7fa26" },
    { name: "Commercial & Co-Working", external_id: "87506e37-09c8-403e-be73-032a4609a696" },
    { name: "Industrial & Logistics", external_id: "81a6c938-32ee-4566-ab9d-8c6eddd77a25" },
    { name: "Future Forecast", external_id: "a7c37a51-b56e-4a72-bf77-5a7529db2f41" },
    { name: "Rental Market Watch", external_id: "1d3cbd90-e97a-432e-b56d-1cdd11855916" },
    { name: "Construction Updates", external_id: "669b1027-c955-4570-a9c8-59aa07a98902" },
    { name: "Legal & Regulatory", external_id: "b182c602-97a8-445e-a074-866d40cf8804" },
    { name: "Mortgage & Financing", external_id: "32a4d01d-05cb-47a6-904b-f71e12428e3d" },
    { name: "Sustainability & Green Living", external_id: "0a8c9d0d-29fb-4985-93f2-3c94e602dde3" },
    { name: "PropTech & AI", external_id: "56d6c5b1-6740-4919-abd2-0ba5efa0ffb2" },
    { name: "Smart Homes & Automation", external_id: "4fadd5c6-c08a-4249-b5d4-0ce23b50521a" },
    { name: "Wellness & Lifestyle", external_id: "52542d6d-e687-47ab-be1d-e9e910283737" },
    { name: "Developer News", external_id: "85081255-4ad1-4f9d-9034-8f57aa72b485" },
    { name: "People", external_id: "996365bf-b3bc-416c-9d6e-e5efd7144941" },
    { name: "Events & Expos", external_id: "48ff44af-13aa-4440-9920-7a954568c037" },
    { name: "Off-Plan & New Launches", external_id: "113607c7-5878-40b8-94af-52025926213e" },
    { name: "Payment Plans & Offers", external_id: "8c6fb4b5-37cf-43e1-ac90-75157bd34aea" },
    { name: "High-Yield & ROI", external_id: "30ea66e9-a7ec-4ed1-96b9-edd9fecd4b6a" },
    { name: "Affordable & Mid-Market", external_id: "3b8ff43b-9c24-4af8-ae73-885fa66c9edb" }
];

const INITIAL_RECIPIENTS = [
    { email: "khizer.saleem@11prop.com", name: "Khizer Saleem Malik" },
    { email: "hammad@11prop.com", name: "Syed Hammad Shah" },
    { email: "anfal@11prop.com", name: "Anfal Gul" },
    { email: "hassaan@11prop.com", name: "Hassaan Sajid" },
    { email: "maliha.khan@11prop.com", name: "Maliha Khan" }
];


// ==========================================
// 2. DROPPING COLLECTIONS
// ==========================================
const collections = [
    "prompts",
    "email_recipients",
    "categories",
    "sources",
    "processed_articles",
    "archived_articles",
    "deleted_articles"
];

collections.forEach(colName => {
    try {
        db.getCollection(colName).drop();
        print(`[+] Dropped collection: ${colName}`);
    } catch (e) {
        print(`[~] Collection ${colName} did not exist, skipping drop.`);
    }
});


// ==========================================
// 3. RECREATING PROMPTS
// ==========================================
print("--- Setting up Prompts ---");
db.prompts.createIndex({ name: 1 }, { unique: false });

INITIAL_PROMPTS.forEach(prompt => {
    prompt._id = uuidv4(); // Assign a string UUID
    prompt.created_at = new Date();
    db.prompts.insertOne(prompt);
    print(`  [+] Added Prompt: ${prompt.name}`);
});


// ==========================================
// 4. RECREATING CATEGORIES
// ==========================================
print("--- Recreating Categories with External IDs ---");
db.categories.createIndex({ name: 1 }, { unique: true });
// New index for fast lookups by postgres ID
db.categories.createIndex({ external_id: 1 }, { unique: true });

INITIAL_CATEGORIES.forEach(cat => {
    db.categories.insertOne({
        _id: uuidv4(),
        name: cat.name,
        external_id: cat.external_id, // Storing the Postgres ID here
        created_at: new Date()
    });
    print(`  [+] Added: ${cat.name} -> ${cat.external_id}`);
});


// ==========================================
// 5. RECREATING RECIPIENTS
// ==========================================
print("--- Setting up Email Recipients ---");
db.email_recipients.createIndex({ email: 1 }, { unique: true });

INITIAL_RECIPIENTS.forEach(recipient => {
    recipient._id = uuidv4();
    recipient.is_active = true;
    recipient.created_at = new Date();
    db.email_recipients.insertOne(recipient);
    print(`  [+] Added Recipient: ${recipient.email}`);
});


// ==========================================
// 6. INITIALIZING OTHER COLLECTIONS (INDEXES ONLY)
// ==========================================
print("--- Initializing Scheduler Collections ---");

// Sources
db.sources.createIndex({ is_active: 1 });
db.sources.createIndex({ listing_url: 1 }, { unique: true });

// Processed Articles
db.processed_articles.createIndex({ url: 1 }, { unique: true });
db.processed_articles.createIndex({ discovered_at: -1 });
db.processed_articles.createIndex({ status: 1 });

// Archives
db.archived_articles.createIndex({ url: 1 }, { unique: true });
db.archived_articles.createIndex({ archived_at: -1 });
db.archived_articles.createIndex({ source_id: 1 });

// Trash
db.deleted_articles.createIndex({ url: 1 }, { unique: true });
db.deleted_articles.createIndex({ deleted_at: -1 });

print("--- 🏁 Initialization Complete ---");