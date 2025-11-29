from langgraph.graph import StateGraph, END
from src.models.MainWorkflowState import MainWorkflowState

# --- Import All Node Functions ---
from src.graph.nodes.load_agent_configuration import load_agent_configuration
from src.graph.nodes.raw_extraction import raw_extraction
from src.graph.nodes.extract_links import extract_links
from src.graph.nodes.summary_generator import generate_summary
from src.graph.nodes.validate_summary import validate_summary
from src.graph.nodes.select_best_summary import select_best_summary
from src.graph.nodes.check_embedded_links import check_embedded_links
from src.graph.nodes.find_other_sources import find_other_sources
from src.graph.nodes.categorize_article import categorize_article
from src.graph.nodes.generate_seo import generate_seo
from src.graph.nodes.notify_webhook import notify_webhook

# --- Import Conditional Edge Function ---
from src.graph.nodes.conditional_edges import check_summary_validity

class MainWorkflow:
    """
    Builds the complete NewsAgent workflow graph.

    This class follows the pattern of your other application,
    encapsulating the graph creation logic.
    """
    def __init__(self):
        """
        Initializes the workflow builder.
        (In this design, our nodes are functions, so no
        instances are needed here, but we could add logging etc.)
        """
        pass

    def create_workflow(self):
        """
        Creates, configures, and compiles the NewsAgent graph.

        Returns:
            The compiled LangGraph application.
        """
        # Create the workflow graph
        builder = StateGraph(MainWorkflowState)

        # 1. Add all the nodes
        builder.add_node("load_agent_configuration", load_agent_configuration)
        builder.add_node("fetch_content", raw_extraction)
        builder.add_node("extract_links", extract_links)
        builder.add_node("generate_summary", generate_summary)
        builder.add_node("validate_summary", validate_summary)
        builder.add_node("select_best_summary", select_best_summary)
        builder.add_node("check_embedded_links", check_embedded_links)
        builder.add_node("find_other_sources", find_other_sources)
        builder.add_node("categorize_article", categorize_article)
        builder.add_node("generate_seo", generate_seo)
        builder.add_node("notify_webhook", notify_webhook)

        # 2. Set the entry point
        builder.set_entry_point("load_agent_configuration")

        # 3. Define the edges (the flow)
        builder.add_edge("load_agent_configuration", "fetch_content")
        builder.add_edge("fetch_content", "extract_links")
        builder.add_edge("extract_links", "generate_summary")
        builder.add_edge("generate_summary", "validate_summary")

        # 4. Define the validation loop
        builder.add_conditional_edges(
            "validate_summary",
            check_summary_validity,
            {
                "regenerate": "generate_summary",  # Loop back
                "end_loop": "select_best_summary"  # Exit loop
            }
        )

        # 5. Define the rest of the flow after the loop
        builder.add_edge("select_best_summary", "check_embedded_links")
        builder.add_edge("check_embedded_links", "find_other_sources")
        builder.add_edge("find_other_sources", "categorize_article")
        builder.add_edge("categorize_article", "generate_seo")
        builder.add_edge("generate_seo", "notify_webhook")

        # 6. Set the final node
        builder.add_edge("notify_webhook", END)

        # 7. Compile and return the app
        app = builder.compile()
        return app