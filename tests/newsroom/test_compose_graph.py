"""
The REAL compose article steps (compose_graph: summary -> categories -> country/names -> why-this-matters
-> SEO -> translation -> reading time -> social) with a fake AI, the init_db prompts and the category
list in a throwaway database (needs TEST_MONGO_URL). Checks that a composed article gets categories
(names and the app's category ids) like any other article, and everything the webhook stores.
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage

import src.graph.nodes.load_agent_configuration as config_node
from src.configs.settings import Settings, settings
from src.graph.nodes.categorize_article import CategorizationModel
from src.graph.nodes.content_enrichment import WhyThisMattersLLMOutput
from src.graph.nodes.generate_seo import SeoLLMOutput
from src.graph.nodes.generate_social_media import SocialCaptionModel
from src.graph.nodes.translate_article import TranslationModel
from src.graph.nodes.validate_summary import ValidationResultModel
from src.models.CountryExtractionModel import CountryAndNamesExtractionModel
from src.newsroom import compose_worker as cw
from src.scripts.init_db import INITIAL_CATEGORIES, seed_categories, seed_prompts

CATEGORIES = ["Developer News", "Off-Plan & New Launches"]
ANSWERS = {
    "ValidationResultModel": ValidationResultModel(is_valid=True, feedback="", semantic_score=9, tone_score=9,
                                                   format_check_passed=True),
    "CategorizationModel": CategorizationModel(categories=CATEGORIES),
    "CountryAndNamesExtractionModel": CountryAndNamesExtractionModel(
        countries=["United Arab Emirates"], companies=["Emaar"], projects=[], places=["Dubai"]),
    "WhyThisMattersLLMOutput": WhyThisMattersLLMOutput(content="[TEST] Why it matters."),
    "SeoLLMOutput": SeoLLMOutput(
        article_title="[TEST] SEO title", h1="[TEST] H1", meta_title="[TEST] Meta", meta_description="[TEST] Desc",
        slug="test-seo-title", primary_keyword="dubai", primary_keywords=["dubai"], og_title="[TEST] OG",
        og_description="[TEST] OG desc", twitter_card_title="[TEST] TW", twitter_card_description="[TEST] TW desc"),
    "TranslationModel": TranslationModel(title_ar="[TEST] عنوان", summary_ar="[TEST] ملخص", content_ar="[TEST] محتوى"),
    "SocialCaptionModel": SocialCaptionModel(headline="[TEST] Headline", caption_body="[TEST] Body",
                                             hashtags=["#Dubai"], call_to_action="[TEST] Read more"),
}


class FakeLLM:
    """Answers every pipeline step without calling OpenAI; records which steps ran."""

    def __init__(self):
        self.calls = []
        self.messages = {}  # the messages each step was given

    async def ainvoke(self, messages):  # the summary step (plain text)
        self.calls.append("summary")
        return AIMessage(content="[TEST] Emaar and DAMAC expand supply in Dubai with two new launches.")

    def with_structured_output(self, schema):
        llm = self

        class Structured:
            async def ainvoke(self, messages):
                llm.calls.append(schema.__name__)
                llm.messages[schema.__name__] = messages
                return ANSWERS[schema.__name__]
        return Structured()


@pytest.fixture
def pipeline(mongo_url, db_name, sync_db, monkeypatch):
    seed_prompts(sync_db)
    seed_categories(sync_db)
    fake = FakeLLM()
    monkeypatch.setattr(Settings, "get_model", lambda self: fake)
    for key, value in {"DATABASE_URL": mongo_url, "MONGO_DB_NAME": db_name, "NEWSROOM_ENABLED": "true"}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(config_node, "_async_config_client", None)

    def run(**kwargs):
        draft = cw.ArticleDraft(title="[TEST] Editor title", content="[TEST] Paragraph one.\n\n[TEST] Paragraph two.")
        texts = [{"title": "Source A", "url": "https://example.com/a", "text": "Emaar launched a tower."},
                 {"title": "Source B", "url": "https://example.com/b", "text": "DAMAC launched villas."}]

        async def go():
            try:
                return await cw.run_article_pipeline("run-1", draft, texts, **kwargs)
            finally:
                await config_node.close_async_config_client()
        return asyncio.run(go())

    run.fake = fake
    return run


def test_composed_article_gets_categories_like_any_article(pipeline):
    article, names = pipeline()
    external_ids = {c["name"]: c["external_id"] for c in INITIAL_CATEGORIES}

    # Categories: the names chosen from the category list and the app's ids for them
    assert article.category == CATEGORIES
    assert article.category_ids == [external_ids[name] for name in CATEGORIES]
    stored = article.model_dump(mode="json")  # what the webhook stores as final_output
    assert stored["category"] == CATEGORIES and stored["category_ids"] == article.category_ids

    # The other steps ran too, in the pipeline's order
    assert pipeline.fake.calls == ["summary", "ValidationResultModel", "CategorizationModel",
                                   "CountryAndNamesExtractionModel", "WhyThisMattersLLMOutput", "SeoLLMOutput",
                                   "TranslationModel", "SocialCaptionModel"]
    assert article.summary.startswith("[TEST] Emaar and DAMAC")
    assert article.countries == ["United Arab Emirates"]
    assert article.author == "Propt Newsroom"
    assert article.title_ar and article.content_ar and article.reading_time
    assert names == {"companies": ["Emaar"], "projects": [], "places": ["Dubai"]}
    assert "@id" not in (article.seo.json_ld_schema.get("mainEntityOfPage") or {})


def test_why_this_matters_gets_the_rules_it_is_given(pipeline):
    rules = cw.NEWSROOM_RULES + "\n\n" + cw.PROMPTS_BY_NAME["newsroom_desk_rental"]["content"]
    article, _ = pipeline(author="Propt Newsroom · Rental Desk", wtm_rules=rules)
    system = dict(pipeline.fake.messages["WhyThisMattersLLMOutput"])["system"]
    assert system.endswith(rules)
    assert article.author == "Propt Newsroom · Rental Desk"
    # Other steps are not given the desk
    assert "SPECIALIST DESK" not in str(pipeline.fake.messages["SeoLLMOutput"])


def test_without_rules_why_this_matters_is_unchanged(pipeline):
    pipeline()
    assert "NEWSROOM RULES" not in str(pipeline.fake.messages["WhyThisMattersLLMOutput"])
