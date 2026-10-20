"""
The fixed lists a signal's fields must come from, the same ones Grok Scout uses, so a
Newsroom signal gets its category and countries (ids and Arabic names) like a Grok signal.

- SIGNAL_CATEGORIES and GCC_REGIONS: the content delivery service's `categories` and
  `regions` tables (propt-content-delivery-be/src/database/database.module.ts), which
  social_scrapper looks names up in when a signal is saved (exact name, any case).
- REAL_ESTATE_CATEGORIES and SIGNAL_ANGLES: social_scrapper's src/config.py and
  src/grok_scout.py (Grok's `categories` and `signal_angle`). The 30 topics are also
  NewsAgent's article categories (init_db.py INITIAL_CATEGORIES; a test keeps them equal).
"""
SIGNAL_CATEGORIES = ["Developer", "Launch", "Construction", "Market", "Social", "Investor", "Viral"]

GCC_REGIONS = ["United Arab Emirates", "Saudi Arabia", "Kuwait", "Oman", "Bahrain", "Qatar"]

SIGNAL_ANGLES = ["Market Movement", "Developer or Investor Implication", "Adjacent Impact"]

REAL_ESTATE_CATEGORIES = [
    "Off-Plan & New Launches", "Payment Plans & Offers", "High-Yield & ROI", "Golden Visa & Residency",
    "Luxury Residences", "Affordable & Mid-Market", "Shared Ownership", "Vocational Holiday Homes",
    "Mega-Projects & Giga-Cities", "Villa, Apartments & Townhouses", "Community Spotlights",
    "Commercial & Co-Working", "Industrial & Logistics", "Future Forecast", "Rental Market Watch",
    "Construction Updates", "Legal & Regulatory", "Mortgage & Financing", "Sustainability & Green Living",
    "PropTech & AI", "Smart Homes & Automation", "Wellness & Lifestyle", "Developer News", "People",
    "Events & Expos", "Architecture & Design Trends", "Interiors & Luxury Fit-Out", "Building Materials & Tech",
    "Landscape & Outdoor Living", "Urban Planning & Infrastructure",
]


def _quoted(values) -> str:
    return "; ".join(f'"{v}"' for v in values)


# The prompt text for these fields (the answer format also only accepts these values).
SIGNAL_FIELD_RULES = f"""- signal_category: exactly one of {_quoted(SIGNAL_CATEGORIES)}.
- signal_angle: exactly one of {_quoted(SIGNAL_ANGLES)}.
- regions: the GCC countries the signal is about, at least one, only from {_quoted(GCC_REGIONS)}.
- categories: 1-3 topics, only from {_quoted(REAL_ESTATE_CATEGORIES)}.
- areas_mentioned: cities, communities and projects; entities_involved: companies (as written in the sources)."""
