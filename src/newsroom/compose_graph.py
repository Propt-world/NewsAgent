"""
The article pipeline for composed articles: the existing nodes from the summary
step onwards. Fetching, link extraction, embedded-link checks and the web search
for other sources are skipped (the chosen sources are used instead), and the
result is stored by compose_worker through the webhook rather than notify_webhook.
The existing graph (src/graph/graph.py) is not changed.
"""
from functools import lru_cache

from langgraph.graph import END, StateGraph

from src.graph.nodes.calculate_reading_time import calculate_reading_time
from src.graph.nodes.categorize_article import categorize_article
from src.graph.nodes.conditional_edges import check_summary_validity
from src.graph.nodes.content_enrichment import enrich_content
from src.graph.nodes.extract_country import extract_country
from src.graph.nodes.generate_seo import generate_seo
from src.graph.nodes.generate_social_media import generate_social_media
from src.graph.nodes.load_agent_configuration import load_agent_configuration
from src.graph.nodes.select_best_summary import select_best_summary
from src.graph.nodes.summary_generator import generate_summary
from src.graph.nodes.translate_article import translate_article
from src.graph.nodes.validate_summary import validate_summary
from src.models.MainWorkflowState import MainWorkflowState


@lru_cache(maxsize=1)
def get_compose_graph():
    builder = StateGraph(MainWorkflowState)
    builder.add_node("load_agent_configuration", load_agent_configuration)
    builder.add_node("generate_summary", generate_summary)
    builder.add_node("validate_summary", validate_summary)
    builder.add_node("select_best_summary", select_best_summary)
    builder.add_node("categorize_article", categorize_article)
    builder.add_node("extract_country", extract_country)
    builder.add_node("content_enrichment", enrich_content)
    builder.add_node("generate_seo", generate_seo)
    builder.add_node("translate_article", translate_article)
    builder.add_node("calculate_reading_time", calculate_reading_time)
    builder.add_node("generate_social_media", generate_social_media)

    builder.set_entry_point("load_agent_configuration")
    builder.add_edge("load_agent_configuration", "generate_summary")
    builder.add_edge("generate_summary", "validate_summary")
    builder.add_conditional_edges(
        "validate_summary",
        check_summary_validity,
        {"regenerate": "generate_summary", "end_loop": "select_best_summary"},
    )
    builder.add_edge("select_best_summary", "categorize_article")
    builder.add_edge("categorize_article", "extract_country")
    builder.add_edge("extract_country", "content_enrichment")
    builder.add_edge("content_enrichment", "generate_seo")
    builder.add_edge("generate_seo", "translate_article")
    builder.add_edge("translate_article", "calculate_reading_time")
    builder.add_edge("calculate_reading_time", "generate_social_media")
    builder.add_edge("generate_social_media", END)
    return builder.compile()
