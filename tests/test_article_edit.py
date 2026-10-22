"""Editing an article's title, summary and why this matters with the pipeline's own translation, SEO and
caption steps (no real AI calls)."""
import asyncio

import pytest

from src.configs.settings import settings
from src.models.AgentPromptsModel import AgentPromptsModel
from src.models.SeoMetadataModel import SeoLLMOutput
from src.models.SocialCaptionModel import SocialCaptionModel
from src.models.TranslationModel import TranslationModel
from src.utils import article_edit

DOC = {
    "_id": "art-1",
    "url": "https://example.com/emaar",
    "final_output": {
        "title": "Emaar launches a tower",
        "title_ar": "إعمار تطلق برجا",
        "content": "Emaar launched a tower in Dubai Hills with 300 homes.",
        "summary": "Emaar launched a tower.",
        "summary_ar": "أطلقت إعمار برجا.",
        "why_this_matters": {"heading": "Why this matters", "content": "It adds supply.", "content_ar": "يضيف معروضا.",
                             "references": []},
        "seo": {"article_title": "Old", "h1": "Old", "meta_title": "Old", "meta_description": "Old", "slug": "old-slug",
                "primary_keyword": "emaar", "primary_keywords": ["emaar"], "og_title": "Old", "og_description": "Old",
                "json_ld_schema": {"mainEntityOfPage": {"@type": "WebPage", "@id": "https://propt.global/news/art-1"},
                                   "image": "https://bucket/img.webp", "datePublished": "2026-10-01"}},
        "social_media": {"headline": "Old", "caption_body": "Old", "hashtags": [], "call_to_action": "Old",
                         "platform_captions": []},
    },
}


class FakeModel:
    def __init__(self):
        self.calls = []

    def with_structured_output(self, output_model):
        fake = self

        class Structured:
            async def ainvoke(self, messages):
                user = messages[1][1]
                fake.calls.append((output_model.__name__, user))
                if output_model is TranslationModel:
                    def section(name):
                        text = user.split(f"---{name}---")[1].split("---")[0].strip()
                        return f"AR({text})" if text else ""
                    return TranslationModel(title_ar=section("TITLE"), summary_ar=section("SUMMARY"),
                                            why_this_matters_ar=section("WHY THIS MATTERS"), content_ar="")
                if output_model is SeoLLMOutput:
                    return SeoLLMOutput(article_title="New", h1="New", meta_title="New", meta_description=user[:40],
                                        slug="ai-slug", primary_keyword="k", primary_keywords=["k"], og_title="New",
                                        og_description="New", twitter_card_title="New", twitter_card_description="New")
                return SocialCaptionModel(headline="New hook", caption_body=user[:40], hashtags=["#a"],
                                          call_to_action="Download", platform_captions=[])
        return Structured()


@pytest.fixture
def fake(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(type(settings), "get_model", lambda self: model)
    prompts = AgentPromptsModel.model_construct(
        translation_system="translate",
        translation_user="---TITLE---\n{title}\n---SUMMARY---\n{summary}\n---WHY THIS MATTERS---\n{why_this_matters}\n---FULL CONTENT---\n{content}",
        seo_system="seo", seo_user="{title} | {summary} | {content_snippet}",
        social_caption_system="social", social_caption_user="{title} | {summary} | {reading_time} {platforms} {cta_target}")

    async def loaded(state):
        return state.model_copy(update={"active_prompts": prompts})
    monkeypatch.setattr(article_edit, "load_agent_configuration", loaded)
    return model


def run(coro):
    return asyncio.run(coro)


def test_translate_uses_the_pipeline_prompts_for_the_changed_parts_only(fake):
    out = run(article_edit.translate(DOC, {"summary": "Emaar sold out.", "why_this_matters": None}))
    assert out == {"summary_ar": "AR(Emaar sold out.)"}
    name, prompt = fake.calls[0]
    assert name == "TranslationModel" and "---TITLE---\n\n" in prompt and prompt.endswith("---FULL CONTENT---\n")
    with pytest.raises(article_edit.EditError, match="Nothing to translate"):
        run(article_edit.translate(DOC, {"title": "  "}))


def test_a_changed_english_part_needs_its_arabic(fake):
    with pytest.raises(article_edit.EditError, match="its Arabic did not"):
        run(article_edit.save(DOC, {"summary": "Emaar sold out."}))
    with pytest.raises(article_edit.EditError, match="its Arabic did not"):
        run(article_edit.save(DOC, {"summary": "Emaar sold out.", "summary_ar": "أطلقت إعمار برجا."}))
    no_arabic = {**DOC, "final_output": {**DOC["final_output"], "summary_ar": ""}}
    with pytest.raises(article_edit.EditError, match="Add the Arabic summary"):
        run(article_edit.save(no_arabic, {"summary": "Emaar sold out."}))
    with pytest.raises(article_edit.EditError, match="Nothing changed"):
        run(article_edit.save(DOC, {"title": "Emaar launches a tower"}))
    with pytest.raises(article_edit.EditError, match="title cannot be empty"):
        run(article_edit.save(DOC, {"title": " ", "title_ar": "x"}))
    assert fake.calls == []  # nothing is regenerated when the edit is refused


def test_why_this_matters_alone_regenerates_nothing(fake):
    fields, regenerated = run(article_edit.save(DOC, {"why_this_matters": "It adds 300 homes.", "why_this_matters_ar": "يضيف 300 منزل."}))
    assert regenerated == [] and fake.calls == []
    wtm = fields["final_output.why_this_matters"]
    assert (wtm["content"], wtm["content_ar"], wtm["heading"]) == ("It adds 300 homes.", "يضيف 300 منزل.", "Why this matters")
    assert set(fields) == {"final_output.why_this_matters"}


def test_a_new_summary_rewrites_seo_and_captions_but_keeps_the_address(fake):
    fields, regenerated = run(article_edit.save(DOC, {"summary": "Emaar sold out.", "summary_ar": "باعت إعمار."}))
    assert regenerated == ["seo", "social_media"]
    assert (fields["final_output.summary"], fields["final_output.summary_ar"]) == ("Emaar sold out.", "باعت إعمار.")
    seo = fields["final_output.seo"]
    assert seo["meta_title"] == "New" and "Emaar sold out." in seo["meta_description"]
    assert seo["slug"] == "old-slug"  # the title did not change: the web address stays
    # The page address and image an image upload set are kept; the headline is new
    ld = seo["json_ld_schema"]
    assert ld["mainEntityOfPage"]["@id"] == "https://propt.global/news/art-1" and ld["image"] == "https://bucket/img.webp"
    assert ld["datePublished"] == "2026-10-01" and ld["headline"] == "New"
    assert fields["final_output.social_media"]["headline"] == "New hook"
    assert [c[0] for c in fake.calls] == ["SeoLLMOutput", "SocialCaptionModel"]
    assert "final_output.title" not in fields


def test_a_new_title_moves_the_address_like_a_title_edit(fake):
    fields, regenerated = run(article_edit.save(DOC, {"title": "Emaar sells out Dubai Hills!", "title_ar": "إعمار تبيع"}))
    assert fields["final_output.seo"]["slug"] == "emaar-sells-out-dubai-hills"
    assert (fields["final_output.title"], fields["final_output.title_ar"]) == ("Emaar sells out Dubai Hills!", "إعمار تبيع")
    assert regenerated == ["seo", "social_media"]


def test_arabic_only_corrections_are_saved_without_ai(fake):
    fields, regenerated = run(article_edit.save(DOC, {"title_ar": "إعمار تطلق برجا جديدا"}))
    assert fields == {"final_output.title": "Emaar launches a tower", "final_output.title_ar": "إعمار تطلق برجا جديدا"}
    assert regenerated == [] and fake.calls == []


def test_prompts_that_cannot_be_loaded_stop_the_edit(monkeypatch):
    async def broken(state):
        return state.model_copy(update={"error_message": "Failed to load agent configuration: summary_system missing"})
    monkeypatch.setattr(article_edit, "load_agent_configuration", broken)
    with pytest.raises(article_edit.ConfigurationError, match="summary_system missing"):
        run(article_edit.translate(DOC, {"title": "New"}))


def test_a_title_only_edit_uses_the_same_address_rule():
    assert article_edit.title_fields("  Emaar sells out Dubai Hills! ") == {
        "final_output.title": "Emaar sells out Dubai Hills!", "final_output.seo.slug": "emaar-sells-out-dubai-hills"}
    with pytest.raises(article_edit.EditError, match="Title cannot be empty."):
        article_edit.title_fields("  ")
