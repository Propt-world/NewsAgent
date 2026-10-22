"""Summary and "why this matters" lengths come from settings through the state: filled into the prompts'
placeholders, and the summary's word count is also checked in code by the critic step."""
from langchain_core.prompts import PromptTemplate

from src.configs.settings import settings
from src.graph.nodes.validate_summary import check_word_count
from src.models.ArticleModel import ArticleModel
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ValidationResultModel import ValidationResultModel
from src.utils.prompt_lengths import fill_lengths, sentence_range

PASSED = ValidationResultModel(is_valid=True, feedback="Looks good.", semantic_score=9.0, format_check_passed=True)
LONG_ARTICLE = "word " * 600


def _state(summary_words=10, article=LONG_ARTICLE, **kw):
    return MainWorkflowState(source_url="https://example.com/a", cleaned_article_text=article,
                             news_article=ArticleModel(title="T", content="C", summary="w " * summary_words), **kw)


def _prompts():
    from src.scripts.init_db import INITIAL_DATA
    return {p["name"]: p["content"] for p in INITIAL_DATA}


def test_the_lengths_come_from_settings():
    state = _state()
    assert (settings.SUMMARY_MIN_WORDS, settings.SUMMARY_MAX_WORDS) == (110, 130)
    assert (settings.WTM_MIN_WORDS, settings.WTM_MAX_WORDS) == (110, 130)
    assert (state.summary_min_words, state.summary_max_words, state.wtm_min_words, state.wtm_max_words) == (110, 130, 110, 130)


def test_a_summary_inside_the_range_keeps_the_critic_s_answer():
    for words in (110, 120, 130):
        assert check_word_count(PASSED, _state(words)) == PASSED


def test_too_short_or_too_long_fails_with_the_count():
    short = check_word_count(PASSED, _state(95))
    assert (short.is_valid, short.format_check_passed) == (False, False)
    assert short.feedback.startswith("Word count: the summary has 95 words; it must be 110-130 words (add about 15 words")
    assert short.feedback.endswith("Looks good.") and short.semantic_score == 9.0
    long = check_word_count(PASSED, _state(150))
    assert not long.is_valid and "(cut about 20 words)" in long.feedback


def test_a_short_article_is_not_padded():
    # The article itself has fewer words than the minimum: only the maximum applies
    assert check_word_count(PASSED, _state(60, article="word " * 80)) == PASSED
    assert not check_word_count(PASSED, _state(140, article="word " * 80)).is_valid


def test_a_different_range_needs_no_code_change():
    state = _state(260, summary_min_words=250, summary_max_words=280, wtm_min_words=90, wtm_max_words=100)
    assert check_word_count(PASSED, state) == PASSED
    system = fill_lengths(_prompts()["summary_system"], state)
    assert "250-280 words (never fewer than 250), written as 12-13 sentences" in system
    assert "contextual insight of 90-100 words" in fill_lengths(_prompts()["content_enrichment_system"], state)


def test_sentence_ranges_follow_the_word_ranges():
    assert sentence_range(110, 130) == "5-6"
    assert sentence_range(250, 280) == "12-13"
    assert sentence_range(20, 22) == "1"


def test_the_prompts_hold_placeholders_not_fixed_lengths():
    prompts, state = _prompts(), _state()
    for name in ("summary_system", "summary_retry_user", "validation_system", "content_enrichment_system",
                 "content_enrichment_user"):
        assert "{summary_min_words}" in prompts[name] or "{wtm_min_words}" in prompts[name], name
        filled = fill_lengths(prompts[name], state)
        assert "{summary_" not in filled and "{wtm_" not in filled and "110-130 words" in filled
    assert "written as 5-6 sentences" in fill_lengths(prompts["summary_system"], state)
    # After filling, the templates ask only for their own inputs
    retry = PromptTemplate.from_template(fill_lengths(prompts["summary_retry_user"], state))
    assert set(retry.input_variables) == {"feedback", "article_text"}
    enrich = PromptTemplate.from_template(fill_lengths(prompts["content_enrichment_user"], state))
    assert set(enrich.input_variables) == {"title", "summary", "categories", "countries", "article_excerpt",
                                           "contextual_sources"}


def test_a_prompt_without_placeholders_is_unchanged():
    assert fill_lengths("Summarise in 100 words.", _state()) == "Summarise in 100 words."
