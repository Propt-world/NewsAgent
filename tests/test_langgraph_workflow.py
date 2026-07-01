from src.graph import graph as graph_module
from src.graph.graph import MainWorkflow
from src.models.MainWorkflowState import MainWorkflowState


EXPECTED_NODE_NAMES = {
    "load_agent_configuration",
    "fetch_content",
    "extract_links",
    "generate_summary",
    "validate_summary",
    "select_best_summary",
    "check_embedded_links",
    "find_other_sources",
    "categorize_article",
    "extract_country",
    "generate_seo",
    "translate_article",
    "calculate_reading_time",
    "generate_social_media",
    "notify_webhook",
}


def test_workflow_compiles_successfully():
    app = MainWorkflow().create_workflow()
    compiled_graph = app.get_graph()

    assert compiled_graph is not None
    assert "__start__" in compiled_graph.nodes
    assert "__end__" in compiled_graph.nodes


def test_workflow_contains_expected_nodes_and_edges():
    app = MainWorkflow().create_workflow()
    compiled_graph = app.get_graph()

    node_keys = set(compiled_graph.nodes.keys())
    assert EXPECTED_NODE_NAMES.issubset(node_keys)

    edges = {(edge.source, edge.target, edge.conditional, edge.data) for edge in compiled_graph.edges}

    assert ("__start__", "load_agent_configuration", False, None) in edges
    assert ("load_agent_configuration", "fetch_content", False, None) in edges
    assert ("fetch_content", "extract_links", False, None) in edges
    assert ("extract_links", "generate_summary", False, None) in edges

    assert ("validate_summary", "generate_summary", True, "regenerate") in edges
    assert ("validate_summary", "select_best_summary", True, "end_loop") in edges

    assert ("generate_social_media", "notify_webhook", False, None) in edges
    assert ("notify_webhook", "__end__", False, None) in edges


def test_workflow_invoke_runs_happy_path_with_mocked_nodes(monkeypatch):
    executed = []

    def _node(name):
        def _run(state):
            executed.append(name)
            return {}

        return _run

    monkeypatch.setattr(graph_module, "load_agent_configuration", _node("load_agent_configuration"))
    monkeypatch.setattr(graph_module, "raw_extraction", _node("fetch_content"))
    monkeypatch.setattr(graph_module, "extract_links", _node("extract_links"))
    monkeypatch.setattr(graph_module, "generate_summary", _node("generate_summary"))
    monkeypatch.setattr(graph_module, "validate_summary", _node("validate_summary"))
    monkeypatch.setattr(graph_module, "select_best_summary", _node("select_best_summary"))
    monkeypatch.setattr(graph_module, "check_embedded_links", _node("check_embedded_links"))
    monkeypatch.setattr(graph_module, "find_other_sources", _node("find_other_sources"))
    monkeypatch.setattr(graph_module, "categorize_article", _node("categorize_article"))
    monkeypatch.setattr(graph_module, "extract_country", _node("extract_country"))
    monkeypatch.setattr(graph_module, "generate_seo", _node("generate_seo"))
    monkeypatch.setattr(graph_module, "translate_article", _node("translate_article"))
    monkeypatch.setattr(graph_module, "calculate_reading_time", _node("calculate_reading_time"))
    monkeypatch.setattr(graph_module, "generate_social_media", _node("generate_social_media"))
    monkeypatch.setattr(graph_module, "notify_webhook", _node("notify_webhook"))
    monkeypatch.setattr(graph_module, "check_summary_validity", lambda state: "end_loop")

    app = MainWorkflow().create_workflow()
    initial_state = MainWorkflowState(source_url="https://example.com/article")

    final_state = app.invoke(initial_state)

    assert isinstance(final_state, dict)
    assert executed[0] == "load_agent_configuration"
    assert "validate_summary" in executed
    assert "notify_webhook" in executed
    assert executed.index("validate_summary") < executed.index("select_best_summary")
    assert executed.index("generate_social_media") < executed.index("notify_webhook")