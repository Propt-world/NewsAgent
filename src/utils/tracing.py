"""
Phoenix tracing (OpenTelemetry with OpenInference), beside Opik until Opik is removed.

Off unless PHOENIX_COLLECTOR_ENDPOINT is set (e.g. http://phoenix:6006/v1/traces): then nothing is
registered and every helper here does nothing, so tracing is never needed for the app to work.

When on, every LangChain / LangGraph call is traced automatically (each graph step and each AI call with its
prompt, answer, tokens, cost and time). A job is wrapped once with job_span(), so all its steps, including
separate graphs and calls, nest under one trace whose top shows the totals. PHOENIX_API_KEY is read by
phoenix.otel when Phoenix has login switched on. Prompt and answer text can be left out later with
OPENINFERENCE_HIDE_INPUTS / OPENINFERENCE_HIDE_OUTPUTS (tokens and cost are still kept).
"""
import atexit
import json
import logging
from contextlib import ExitStack, contextmanager
from typing import Iterator, List, Optional

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from src.configs.settings import settings

logger = logging.getLogger(__name__)

_provider = None


def _tracer():
    """Phoenix's tracer when on; otherwise one that records nothing."""
    return _provider.get_tracer("newsagent") if _provider is not None else trace.get_tracer("newsagent")


def tracing_enabled() -> bool:
    return bool((settings.PHOENIX_COLLECTOR_ENDPOINT or "").strip())


def init_tracing() -> bool:
    """Registers Phoenix tracing once per process (call it at start, before the first AI call). Returns True if on."""
    global _provider
    if _provider is not None:
        return True
    if not tracing_enabled():
        return False
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register

        _provider = register(
            endpoint=settings.PHOENIX_COLLECTOR_ENDPOINT.strip(),  # ".../v1/traces" sends over HTTP
            project_name=settings.PHOENIX_PROJECT_NAME,
            batch=True,
            verbose=False,
        )
        LangChainInstrumentor().instrument(tracer_provider=_provider)
        # Short scripts would exit before the last traces are sent: send them on any exit
        atexit.register(flush_tracing)
        logger.info(f"[TRACING] Phoenix tracing registered (project {settings.PHOENIX_PROJECT_NAME})")
        print(f"[TRACING] Phoenix tracing registered (project {settings.PHOENIX_PROJECT_NAME})")
        return True
    except Exception as e:
        # A tracing problem must never stop the app.
        logger.warning(f"[TRACING] Phoenix tracing could not start: {e}")
        _provider = None
        return False


def flush_tracing() -> None:
    """Sends what is still waiting (call before a short script or a stopping worker exits)."""
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception as e:
            logger.warning(f"[TRACING] Flush failed: {e}")


@contextmanager
def job_span(name: str, *, session_id: Optional[str] = None, metadata: Optional[dict] = None,
             tags: Optional[List[str]] = None, input_value: Optional[str] = None) -> Iterator[Span]:
    """
    One trace for a whole job (an article, a compose run): everything inside nests under it, and Phoenix
    shows its total tokens, cost and time. session_id groups traces (a compose run's id). Errors raised inside
    mark it failed; for failures that are not exceptions call mark_failed(span, message).
    Without tracing it yields a span that records nothing.
    """
    from openinference.instrumentation import using_metadata, using_session, using_tags
    from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

    attributes = {SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value}
    if input_value is not None:
        attributes[SpanAttributes.INPUT_VALUE] = input_value
    if session_id:
        attributes[SpanAttributes.SESSION_ID] = session_id
    if metadata:
        attributes[SpanAttributes.METADATA] = json.dumps(metadata, default=str, ensure_ascii=False)
    if tags:
        attributes[SpanAttributes.TAG_TAGS] = tags
    with ExitStack() as stack:
        # Also put on every step inside (the LangChain tracing reads these)
        if session_id:
            stack.enter_context(using_session(session_id))
        if metadata:
            stack.enter_context(using_metadata(metadata))
        if tags:
            stack.enter_context(using_tags(tags))
        with _tracer().start_as_current_span(name, attributes=attributes) as span:
            yield span


@contextmanager
def embedding_span(model: str, text: str) -> Iterator[Span]:
    """One embedding call made with the OpenAI library directly (not through LangChain, so not traced
    automatically): the model and the text; add the tokens with set_token_counts() once the answer is back.
    Marked as an LLM call: Phoenix counts tokens and cost only on those. Its price list has no embedding models,
    so the model's price is added once per Phoenix (deploy/phoenix/add_model_prices.sh)."""
    from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

    attributes = {
        SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.LLM.value,
        SpanAttributes.EMBEDDING_MODEL_NAME: model,
        SpanAttributes.LLM_MODEL_NAME: model,  # Phoenix works out the cost from the model and the tokens
        SpanAttributes.LLM_PROVIDER: "openai",
        SpanAttributes.LLM_SYSTEM: "openai",
        SpanAttributes.INPUT_VALUE: text,
    }
    with _tracer().start_as_current_span("openai.embeddings", attributes=attributes) as span:
        yield span


def set_token_counts(span: Span, prompt: int, completion: int = 0) -> None:
    from openinference.semconv.trace import SpanAttributes

    span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, int(prompt or 0))
    span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, int(completion or 0))
    span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_TOTAL, int(prompt or 0) + int(completion or 0))


def set_output(span: Span, value) -> None:
    """The job's result, shown at the top of its trace."""
    from openinference.semconv.trace import SpanAttributes

    span.set_attribute(SpanAttributes.OUTPUT_VALUE, value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False))


def mark_failed(span: Span, message: str) -> None:
    span.set_status(Status(StatusCode.ERROR, (message or "failed")[:500]))
