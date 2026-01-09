// JavaScript file to be executed with mongosh
// Usage: mongosh <connection_string> src/scripts/insert_country_prompts.js

print("--- Inserting Country Extraction Prompts ---");

var dbName = "newsagent"; // Default database name, change if needed or rely on connection string
var db = db.getSiblingDB(dbName);

var prompts = [
    {
        "name": "country_extraction_system",
        "content": `You are an expert in geographical entity extraction.
your task is to identify and extract the country or countries that the news article is primarily about.

RULES:
1. Extract only the countries that are central to the news story.
2. If the article mentions a city or state, extract the corresponding country.
3. If no specific country is relevant (e.g., general tech news), return an empty list.
4. Output must be a list of country names in English.
5. Do not include regions (like "Middle East") unless a specific country is not applicable.
6. Normalize country names (e.g., "UAE" -> "United Arab Emirates", "US" -> "United States").`,
        "input_variables": [],
        "description": "System instruction for country extraction.",
        "version": "v1.0",
        "status": "active",
        "created_at": new Date()
    },
    {
        "name": "country_extraction_user",
        "content": `Please extract the relevant countries from the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---CONTENT SNIPPET---
{content}`,
        "input_variables": ["title", "summary", "content"],
        "description": "User prompt for country extraction.",
        "version": "v1.0",
        "status": "active",
        "created_at": new Date()
    }
];

prompts.forEach(function (prompt) {
    var result = db.prompts.updateOne(
        { name: prompt.name },
        { $set: prompt },
        { upsert: true }
    );

    if (result.upsertedCount > 0) {
        print("  [+] Inserted: " + prompt.name);
    } else if (result.modifiedCount > 0) {
        print("  [~] Updated: " + prompt.name);
    } else {
        print("  [=] No change: " + prompt.name);
    }
});

print("--- Done ---");
