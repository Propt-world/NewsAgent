"""The summary kept after the critic's retries: one the critic passed wins over a rejected one."""
from src.graph.nodes.select_best_summary import select_best_summary
from src.models.ArticleModel import ArticleModel
from src.models.MainWorkflowState import MainWorkflowState
from src.models.SummaryAttemptModel import SummaryAttemptModel
from src.models.ValidationResultModel import ValidationResultModel


def _attempt(summary, is_valid, semantic):
    return SummaryAttemptModel(summary=summary, validation=ValidationResultModel(is_valid=is_valid, semantic_score=semantic))


def _state(*attempts):
    return MainWorkflowState(source_url="https://example.com/a", news_article=ArticleModel(title="T", content="C"),
                             summary_attempts=list(attempts))


def test_a_valid_summary_wins_over_a_rejected_one_with_a_higher_score():
    # Dev, 2026-10-21: a rejected summary with a wrong year scored higher on meaning and was kept
    result = select_best_summary(_state(_attempt("wrong year", False, 9.5), _attempt("correct", True, 8.5)))
    assert result.news_article.summary == "correct"
    assert result.validation_result.is_valid


def test_without_a_valid_summary_the_highest_score_is_kept():
    result = select_best_summary(_state(_attempt("a", False, 6.0), _attempt("b", False, 7.5), _attempt("c", False, None)))
    assert result.news_article.summary == "b"


def test_among_valid_summaries_the_highest_score_is_kept():
    result = select_best_summary(_state(_attempt("a", True, 8.0), _attempt("b", True, 9.0), _attempt("c", False, 9.9)))
    assert result.news_article.summary == "b"
