"""
Specialist desks for composed articles (optional; articles only).

A desk is a subject lens: what the researcher and writer look for and lead with, and whose point of view
"why this matters" takes. Facts still come only from the sources. With no desk the general writer is used.
The byline names the desk ("Propt Newsroom · Rental Desk"); the compose window suggests a desk from the
sources' topics (a hint only, never chosen automatically).
"""
from collections import Counter
from typing import Dict, Iterable, List, Optional

AUTHOR = "Propt Newsroom"

# key -> label, readers, focus, the article topics (REAL_ESTATE_CATEGORIES) that suggest it
DESKS: Dict[str, dict] = {
    "sales": {
        "label": "Sales & Launches",
        "readers": "buyers and off-plan investors",
        "focus": "launches, units sold, prices and price per square foot, payment plans, how fast a project sold, "
                 "handover dates",
        "topics": ["Off-Plan & New Launches", "Payment Plans & Offers", "Villa, Apartments & Townhouses"],
    },
    "rental": {
        "label": "Rental",
        "readers": "landlords, tenants and buy-to-let investors",
        "focus": "rents and rent changes, rental yields, occupancy and tenant demand, rent rules (rental index, "
                 "Ejari), short-term and holiday-home lets",
        "topics": ["Rental Market Watch", "Vocational Holiday Homes"],
    },
    "finance": {
        "label": "Investment & Finance",
        "readers": "investors",
        "focus": "returns and ROI, mortgages and interest rates, funding and financing deals, REITs and listings, "
                 "developers' financial results, investor flows",
        "topics": ["High-Yield & ROI", "Mortgage & Financing"],
    },
    "market": {
        "label": "Market Data & Trends",
        "readers": "investors and market watchers",
        "focus": "transaction volumes and values, price indices, changes over periods (month, quarter, year), "
                 "supply figures, what the numbers show",
        "topics": ["Future Forecast"],
    },
    "policy": {
        "label": "Policy & Regulation",
        "readers": "buyers, owners and investors affected by the rules",
        "focus": "new laws and rules, visas and residency, fees, what changes, for whom and from when",
        "topics": ["Golden Visa & Residency", "Legal & Regulatory"],
    },
    "projects": {
        "label": "Projects & Construction",
        "readers": "buyers waiting for homes and people following big projects",
        "focus": "construction progress, contractors and contract values, timelines and delays, infrastructure "
                 "and transport links, master plans",
        "topics": ["Mega-Projects & Giga-Cities", "Construction Updates", "Urban Planning & Infrastructure",
                   "Building Materials & Tech"],
    },
    "commercial": {
        "label": "Commercial & Industrial",
        "readers": "businesses and commercial-property investors",
        "focus": "offices, retail, co-working, logistics and warehouses, hotels: space, rents, occupancy, deals",
        "topics": ["Commercial & Co-Working", "Industrial & Logistics"],
    },
    "luxury": {
        "label": "Luxury & Prime",
        "readers": "high-end buyers and investors",
        "focus": "prime areas, record deals, branded residences, top-end prices and finishes",
        "topics": ["Luxury Residences", "Interiors & Luxury Fit-Out"],
    },
    "communities": {
        "label": "Communities & Living",
        "readers": "residents and families choosing where to live",
        "focus": "the community itself: amenities, schools, transport, green spaces, everyday living costs, "
                 "affordable and mid-market homes",
        "topics": ["Community Spotlights", "Affordable & Mid-Market", "Wellness & Lifestyle",
                   "Sustainability & Green Living", "Landscape & Outdoor Living"],
    },
}
DESK_KEYS = tuple(DESKS)


def desk_prompt(key: str) -> str:
    """The desk's rules (the registry prompt newsroom_desk_<key>); no template inputs."""
    d = DESKS[key]
    return (f"SPECIALIST DESK: {d['label'].upper()}.\n"
            f"Readers: {d['readers']}.\n"
            f"Give prominence to these points when the sources contain them: {d['focus']}.\n"
            "Explain what they mean for these readers. Use only facts and figures from the sources; "
            "if the sources say little about these points, do not fill the gap.")


def byline(key: Optional[str]) -> str:
    return f"{AUTHOR} · {DESKS[key]['label']} Desk" if key else AUTHOR


def is_newsroom_byline(author) -> bool:
    return isinstance(author, str) and (author == AUTHOR or author.startswith(f"{AUTHOR} · "))


TOPIC_DESK = {topic: key for key, d in DESKS.items() for topic in d["topics"]}


def suggest_desk(topics_per_source: Iterable[Iterable[str]]) -> Optional[dict]:
    """The desk most of the sources' topics point to (counted once per source), or None."""
    votes, seen_topics = Counter(), {}
    for topics in topics_per_source:
        desks = set()
        for topic in topics or []:
            key = TOPIC_DESK.get(topic)
            if key:
                desks.add(key)
                seen_topics.setdefault(key, []).append(topic)
        votes.update(desks)
    if not votes:
        return None
    # Most sources first; ties go to the desk listed first
    key = max(votes, key=lambda k: (votes[k], -DESK_KEYS.index(k)))
    topics = list(dict.fromkeys(seen_topics[key]))
    sources = votes[key]
    return {"desk": key, "label": DESKS[key]["label"],
            "reason": f"{', '.join(topics)} ({sources} source{'s' if sources != 1 else ''})"}


def desk_options() -> List[dict]:
    return [{"desk": k, "label": d["label"], "readers": d["readers"]} for k, d in DESKS.items()]
