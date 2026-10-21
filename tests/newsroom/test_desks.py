"""Specialist desks for composed articles (no database needed)."""
from src.newsroom import desks
from src.newsroom.pipeline import newsroom_composed
from src.newsroom.prompts import EDITOR_RULES, NEWSROOM_RULES, PROMPTS_BY_NAME
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
        assert content.endswith(desks.DESK_RULES) and "STAGE RESPONSIBILITIES" in content
        assert "NEWSROOM RULES" not in content  # every composed article gets those already; not repeated


def test_every_composed_article_follows_the_newsroom_rules():
    for rule in ("Never give investment, legal or financial advice", "Separate established facts from claims",
                 "Avoid promotional language", "Do not calculate new figures", "Never imply firsthand visits"):
        assert rule in NEWSROOM_RULES
    assert NEWSROOM_RULES in PROMPTS_BY_NAME["newsroom_writer_article"]["content"]
    assert NEWSROOM_RULES in EDITOR_RULES and "list each fix" in EDITOR_RULES


def test_the_editor_checks_the_structure_too():
    """From the first real dev compose (2026-10-21): a reworded source lead, retold sources and a wrap-up ending."""
    for check in ("STRUCTURE CHECKS", "First sentence: the hook", "Copying:", "more than eight",
                  "Ending: delete a final summary", "Headline: describes each figure as the sources do"):
        assert check in EDITOR_RULES, check
    writer = PROMPTS_BY_NAME["newsroom_writer_article"]["content"]
    assert "do not retell one source and then the next" in writer and "never relabel it" in writer


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
    assert desks.suggest_desk([["Off-Plan & New Launches"], ["Rental Market Watch"]]) is None  # a tie: no suggestion
    assert desks.suggest_desk([["Developer News", "People"], []]) is None


def test_no_suggestion_when_no_desk_covers_two_sources():
    # The first real dev compose (2026-10-21): five desks with one source each went to the desk listed first
    dubai = ["Mega-Projects & Giga-Cities", "Urban Planning & Infrastructure", "Future Forecast", "Developer News"]
    sharjah = ["High-Yield & ROI", "Off-Plan & New Launches", "Rental Market Watch", "Developer News"]
    assert desks.suggest_desk([dubai, sharjah]) is None
    # One source: its strongest desk, by matching topics
    assert desks.suggest_desk([dubai])["desk"] == "projects"
    assert desks.suggest_desk([["Off-Plan & New Launches", "Rental Market Watch"]]) is None
    # Two sources sharing two desks: the one with more matching topics
    both = desks.suggest_desk([["Off-Plan & New Launches", "Payment Plans & Offers", "Rental Market Watch"],
                               ["Off-Plan & New Launches", "Rental Market Watch"]])
    assert (both["desk"], both["reason"]) == ("sales", "Off-Plan & New Launches, Payment Plans & Offers (2 sources)")


def test_topic_warning_when_the_sources_share_no_desk():
    dubai = ["Mega-Projects & Giga-Cities", "Future Forecast", "Developer News"]
    sharjah = ["High-Yield & ROI", "Off-Plan & New Launches"]
    warning = desks.topic_warning([("Dubai", dubai), ("Sharjah", sharjah)])
    assert '"Dubai": Market Data & Trends, Projects & Construction' in warning
    assert '"Sharjah": Sales & Launches, Investment & Finance' in warning and "compose anyway" in warning
    # Launch, sale and sold-out stories share the Sales & Launches desk
    assert desks.topic_warning([("A", ["Off-Plan & New Launches"]), ("B", ["Payment Plans & Offers", "Future Forecast"])]) is None
    # Items whose topics match no desk cannot be judged
    assert desks.topic_warning([("A", dubai), ("B", ["Developer News"]), ("C", [])]) is None
    assert desks.topic_warning([("A", dubai)]) is None


def test_byline_names_the_desk():
    assert desks.byline(None) == "Propt Newsroom"
    assert desks.byline("communities") == "Propt Newsroom · Communities & Living Desk"
    assert desks.is_newsroom_byline("Propt Newsroom · Rental Desk")
    assert not desks.is_newsroom_byline("Propt Newsroomy") and not desks.is_newsroom_byline(None)


def test_the_webhook_still_recognises_composed_articles_with_a_desk_byline():
    good = {"generated_from": "run-1", "source_id": "newsroom_compose", "author": "Propt Newsroom · Rental Desk"}
    assert newsroom_composed("newsroom://compose/run-1", good)
    assert not newsroom_composed("newsroom://compose/run-1", {**good, "author": "Someone Else"})
