var db = db.getSiblingDB("newsagent");

var prompts = [
    {
        "name": "social_caption_system",
        "content": `You are a world-class Direct Response Copywriter and Social Media Strategist. 
Your goal is to drive high click-through rates (CTR) and App Downloads.

TONE:
- Urgent, engaging, and professional but accessible.
- Use psychological triggers (FOMO, curiosity, value).
- Avoid passive voice. Be punchy.

OBJECTIVE:
- Summarize the news hook instantly.
- Make the reader feel they *must* read the full story or use the app to stay ahead.
- The Call to Action (CTA) must be strong and directive (e.g., "Download now", "Read full report").`,
        "input_variables": [],
        "description": "System instruction for social media copywriter.",
        "version": "v1.0",
        "status": "active",
        "created_at": new Date()
    },
    {
        "name": "social_caption_user",
        "content": `Create a high-conversion social media post for this article:

TITLE: {title}
SUMMARY: {summary}
READING TIME: {reading_time} mins

REQUIREMENTS:
1. HEADLINE: A scroll-stopping hook (max 10 words).
2. BODY: Max one paragraph (2-3 sentences) explaining *why* this matters.
3. CTA: Direct users to download the 'Propt App' for the full analysis.
4. HASHTAGS: Mix of broad and niche real estate/business tags.`,
        "input_variables": ["title", "summary", "reading_time"],
        "description": "User prompt for generating captions.",
        "version": "v1.0",
        "status": "active",
        "created_at": new Date()
    }
];

// Insert or Update
prompts.forEach(function (p) {
    db.prompts.updateOne({ name: p.name }, { $set: p }, { upsert: true });
    print("Updated: " + p.name);
});