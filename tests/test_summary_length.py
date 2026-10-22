"""The summary's word count is checked in code (state.summary_min_words-summary_max_words), not by the critic AI."""
from src.configs.settings import settings
from src.graph.nodes.validate_summary import check_word_count
from src.models.ArticleModel import ArticleModel
from src.models.MainWorkflowState import MainWorkflowState
from src.models.ValidationResultModel import ValidationResultModel

PASSED = ValidationResultModel(is_valid=True, feedback="Looks good.", semantic_score=9.0, format_check_passed=True)
LONG_ARTICLE = "word " * 600


def _state(summary_words, article=LONG_ARTICLE, **kw):
    return MainWorkflowState(source_url="https://example.com/a", cleaned_article_text=article,
                             news_article=ArticleModel(title="T", content="C", summary="w " * summary_words), **kw)


def test_the_range_comes_from_settings():
    state = _state(10)
    assert (state.summary_min_words, state.summary_max_words) == (settings.SUMMARY_MIN_WORDS, settings.SUMMARY_MAX_WORDS)
    assert (settings.SUMMARY_MIN_WORDS, settings.SUMMARY_MAX_WORDS) == (250, 280)


def test_a_summary_inside_the_range_keeps_the_critic_s_answer():
    for words in (250, 265, 280):
        assert check_word_count(PASSED, _state(words)) == PASSED


def test_too_short_or_too_long_fails_with_the_count():
    short = check_word_count(PASSED, _state(201))
    assert (short.is_valid, short.format_check_passed) == (False, False)
    assert short.feedback.startswith("Word count: the summary has 201 words; it must be 250-280 words (add about 49 words")
    assert short.feedback.endswith("Looks good.") and short.semantic_score == 9.0
    long = check_word_count(PASSED, _state(300))
    assert not long.is_valid and "(cut about 20 words)" in long.feedback


def test_a_short_article_is_not_padded():
    # The article itself has fewer words than the minimum: only the maximum applies
    assert check_word_count(PASSED, _state(150, article="word " * 200)) == PASSED
    assert not check_word_count(PASSED, _state(290, article="word " * 200)).is_valid


def test_the_range_can_be_set_on_the_state():
    assert check_word_count(PASSED, _state(120, summary_min_words=100, summary_max_words=130)) == PASSED
