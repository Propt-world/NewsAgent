"""Specialist desks for composed articles (no database needed)."""
from src.newsroom import desks
from src.newsroom.pipeline import newsroom_composed
from src.newsroom.prompts import PROMPTS_BY_NAME
from src.newsroom.signal_options import REAL_ESTATE_CATEGORIES


def test_desk_topics_are_real_topics_and_each_belongs_to_one_desk():
    topics = [t for d in desks.DESKS.values() for t in d["topics"]]
    assert set(topics) <= set(REAL_ESTATE_CATEGORIES)
    assert len(topics) == len(set(topics))
    assert len(desks.DESKS) == 9 and "communities" in desks.DESKS


def test_every_desk_has_a_registry_prompt_without_template_inputs():
    for key, desk in desks.DESKS.items():
        content = PROMPTS_BY_NAME[f"newsroom_desk_{key}"]["content"]
        assert PROMPTS_BY_NAME[f"newsroom_desk_{key}"]["input_variables"] == [] and "{" not in content
        assert content.startswith(f"SPECIALIST DESK: {desk['label'].upper()}\n")
        # Each desk has the parts of revision 3, then the rules every desk shares
        for part in ("READERS:", "VOICE:", "LOOK FOR:", "ALWAYS CHECK", "LEAD:", "BODY ORDER:", "NEVER:",
                     "TERMS (approved meanings):", "WHY THIS MATTERS:", "STYLE EXAMPLE:"):
            assert part in content, (key, part)
        assert content.endswith(desks.DESK_RULES)
    for rule in ("Never give investment, legal or financial advice", "Do not calculate new figures",
                 "Never imply firsthand visits", "STAGE RESPONSIBILITIES"):
        assert rule in desks.DESK_RULES


def test_every_composed_article_follows_the_news_structure():
    """The general writer (with or without a desk) gets the inverted pyramid, with the lead as the hook."""
    writer = PROMPTS_BY_NAME["newsroom_writer_article"]["content"]
    for part in ("ARTICLE STRUCTURE (inverted pyramid", "Headline (the title)", "Lead (first paragraph", "the hook",
                 "Nut graph", "Body:", "Quotes and attribution", "Ending:", "Never a summary",
                 "6-10 paragraphs of 1-3 sentences", "BEFORE YOU FINISH", "SOURCE RULES"):
        assert part in writer, part
    assert "Find the angle" in PROMPTS_BY_NAME["newsroom_researcher"]["content"]
    assert "ARTICLE STRUCTURE" not in desks.DESK_RULES  # not repeated in the desk prompts


def test_suggestion_counts_each_source_once_and_needs_a_matching_topic():
    rental = desks.suggest_desk([["Rental Market Watch", "Vocational Holiday Homes"], ["Rental Market Watch"],
                                 ["Off-Plan & New Launches"]])
    assert (rental["desk"], rental["reason"]) == ("rental", "Rental Market Watch, Vocational Holiday Homes (2 sources)")
    tie = desks.suggest_desk([["Off-Plan & New Launches"], ["Rental Market Watch"]])
    assert tie["desk"] == "sales"  # a tie goes to the desk listed first
    assert desks.suggest_desk([["Developer News", "People"], []]) is None


def test_byline_names_the_desk():
    assert desks.byline(None) == "Propt Newsroom"
    assert desks.byline("communities") == "Propt Newsroom · Communities & Living Desk"
    assert desks.is_newsroom_byline("Propt Newsroom · Rental Desk")
    assert not desks.is_newsroom_byline("Propt Newsroomy") and not desks.is_newsroom_byline(None)


def test_the_webhook_still_recognises_composed_articles_with_a_desk_byline():
    good = {"generated_from": "run-1", "source_id": "newsroom_compose", "author": "Propt Newsroom · Rental Desk"}
    assert newsroom_composed("newsroom://compose/run-1", good)
    assert not newsroom_composed("newsroom://compose/run-1", {**good, "author": "Someone Else"})
