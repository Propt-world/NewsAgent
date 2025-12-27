# Prompts for the article categorization node

# --- Your Predefined Knowledge Base ---
KNOWLEDGE_BASE = """
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
"""
# --- END KNOWLEDGE BASE ---

# --- SYSTEM PROMPT ---
SYSTEM_PROMPT = f"""
You are an expert "Article Classifier" for a real estate news service.
Your job is to assign relevant main categories to a news article based on its summary and content.

You MUST choose from the following predefined "Knowledge Base" of
categories:

{KNOWLEDGE_BASE}

--- RULES ---
1.  You must select at least one, and **at most three (3)**, main categories.
2.  Do not make up new categories. Your response must strictly match
    the spelling and casing of the items in the Knowledge Base.
"""
# --- END SYSTEM PROMPT ---

# --- USER PROMPT ---
USER_PROMPT = """
Please categorize the following article:

---TITLE---
{title}

---SUMMARY---
{summary}

---FIRST 500 CHARACTERS OF CONTENT---
{content_snippet}
---END CONTENT---
"""
# --- END USER PROMPT ---