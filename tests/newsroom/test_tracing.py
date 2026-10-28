"""Phoenix tracing: one trace per job, every step and AI call nested under it, off without an endpoint."""
import asyncio
from typing import TypedDict

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.graph import END, START, StateGraph
from opentelemetry.trace import StatusCode

from src.configs.settings import settings
from src.utils import tracing


def test_off_without_an_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "PHOENIX_COLLECTOR_ENDPOINT", "")  # docker compose passes a blank value as ""
    monkeypatch.setattr(tracing, "_provider", None)
    assert tracing.init_tracing() is False
    with tracing.job_span("article_pipeline", session_id="x", metadata={"a": 1}) as span:
        tracing.set_output(span, {"outcome": "completed"})  # records nothing, never fails
    assert not span.is_recording()


class State(TypedDict):
    text: str


def _two_step_graph():
    """Two steps, each one AI call: like the article and compose steps."""
    model = FakeListChatModel(responses=["Emaar sold out the tower.", "باعت إعمار البرج."])

    async def summary(state):
        return {"text": (await model.ainvoke(f"Summarise: {state['text']}")).content}

    async def translate(state):
        return {"text": (await model.ainvoke(f"Translate: {state['text']}")).content}

    graph = StateGraph(State)
    graph.add_node("summary", summary)
    graph.add_node("translate", translate)
    graph.add_edge(START, "summary")
    graph.add_edge("summary", "translate")
    graph.add_edge("translate", END)
    return graph.compile()


def test_every_step_nests_under_one_job_trace(trace_spans):
    graph = _two_step_graph()

    async def job():
        with tracing.job_span("compose_compose", session_id="run-1", tags=["newsroom", "compose"],
                              metadata={"compose_run_id": "run-1"}, input_value="article:A1") as span:
            # Two separate pieces of one job (as compose runs its writer, then the article steps)
            await graph.ainvoke({"text": "Emaar launches a tower."})
            await FakeListChatModel(responses=["Checked."]).ainvoke("Check it")
            tracing.set_output(span, {"status": "succeeded"})
    asyncio.run(job())

    spans = trace_spans.get_finished_spans()
    root = next(s for s in spans if s.name == "compose_compose")
    assert root.parent is None and root.status.status_code != StatusCode.ERROR
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}  # one trace, not three
    llm_calls = [s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"]
    assert len(llm_calls) == 3  # each AI call once (no double counting)
    steps = {s.name for s in spans}
    assert {"summary", "translate"} <= steps  # each graph step is its own span
    # The run id groups them, and each call keeps its prompt and answer
    assert all(s.attributes.get("session.id") == "run-1" for s in llm_calls)
    # Every step carries the job's name: the LLM Observability page adds up cost per kind of job with it
    assert all('"job": "compose_compose"' in s.attributes.get("metadata", "") for s in llm_calls)
    assert any("Emaar sold out the tower." in str(s.attributes.get("output.value")) for s in llm_calls)
    assert root.attributes["session.id"] == "run-1" and '"status": "succeeded"' in root.attributes["output.value"]


def test_a_failure_marks_the_job(trace_spans):
    with tracing.job_span("article_pipeline") as span:
        tracing.mark_failed(span, "Summary failed the checks")
    try:
        with tracing.job_span("compose_compose"):
            raise RuntimeError("model down")
    except RuntimeError:
        pass
    failed = {s.name: s.status for s in trace_spans.get_finished_spans()}
    assert failed["article_pipeline"].status_code == StatusCode.ERROR
    assert failed["article_pipeline"].description == "Summary failed the checks"
    assert failed["compose_compose"].status_code == StatusCode.ERROR


def test_article_job_outcome_is_shown(trace_spans):
    from src.worker import _trace_article_outcome

    outcomes = [{"error_message": "Summary failed the checks"},
                {"real_estate_check": {"is_real_estate": False, "reason": "A food fair"}},
                {"news_article": None}]
    for state in outcomes:
        with tracing.job_span("article_pipeline") as span:
            _trace_article_outcome(span, state)
    failed, skipped, done = trace_spans.get_finished_spans()
    assert failed.status.status_code == StatusCode.ERROR and "Summary failed" in failed.attributes["output.value"]
    assert "skipped: not real estate" in skipped.attributes["output.value"]
    assert '"outcome": "completed"' in done.attributes["output.value"] and done.status.status_code != StatusCode.ERROR


def test_article_timeout_is_explained_and_only_the_last_attempt_fails(trace_spans, monkeypatch):
    from src import worker

    class SlowGraph:
        async def ainvoke(self, state, config=None):
            await asyncio.sleep(1)

    monkeypatch.setattr(worker, "WORKFLOW_TIMEOUT_SECONDS", 0.01)
    for attempts_before in (0, 2):  # the first of 3 attempts, then the last
        try:
            asyncio.run(worker._run_article_job(SlowGraph(), {}, {}, "job-1", "https://example.com/a",
                                                {"attempt": attempts_before}, 3))
            raise AssertionError("the timeout must reach the worker, which retries or gives up")
        except asyncio.TimeoutError:
            pass
    retried, last = trace_spans.get_finished_spans()
    # A first attempt that ran out of time is tried again: not a failed job
    assert retried.status.status_code != StatusCode.ERROR
    assert "Took longer than 0.01 s (attempt 1 of 3); tried again" in retried.attributes["output.value"]
    # The last attempt is a failure, with the reason instead of a blank "TimeoutError:"
    assert last.status.status_code == StatusCode.ERROR
    assert last.status.description == "Took longer than 0.01 s (attempt 3 of 3); moved to the failed list"


def test_scripts_send_their_last_traces_on_exit(monkeypatch):
    """A short script exits before the batch is sent: init_tracing registers a send on exit."""
    import atexit

    import phoenix.otel
    from openinference.instrumentation.langchain import LangChainInstrumentor

    class FakeProvider:  # no real connection: nothing outlives this test
        def get_tracer(self, name):
            from opentelemetry import trace
            return trace.NoOpTracer()

    made, registered = [], []
    monkeypatch.setattr(phoenix.otel, "register", lambda **kw: made.append(kw) or FakeProvider())
    monkeypatch.setattr(LangChainInstrumentor, "instrument", lambda self, **kw: None)
    monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(settings, "PHOENIX_COLLECTOR_ENDPOINT", " http://phoenix:6006/v1/traces ")
    monkeypatch.setattr(settings, "PHOENIX_PROJECT_NAME", "newsagent/dev")
    monkeypatch.setattr(tracing, "_provider", None)
    assert tracing.init_tracing() is True
    assert tracing.flush_tracing in registered
    assert made == [{"endpoint": "http://phoenix:6006/v1/traces", "project_name": "newsagent/dev",
                     "batch": True, "verbose": False}]
