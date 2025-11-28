import os
from datetime import datetime
from langchain_core.runnables.graph import MermaidDrawMethod
from src.graph.graph import MainWorkflow


def generate_workflow_graph(
    xray: bool = True,
    output_dir: str = ".",
) -> tuple[str, str]:
    """
    Build the LangGraph workflow and render its visualization.

    Args:
        xray: Whether to include subgraph details.
        output_dir: Directory where the PNG file will be written.

    Returns:
        A tuple of:
            - mermaid_syntax: The Mermaid text representation of the graph.
            - file_path: Absolute path to the generated PNG file.
    """
    # Initialize the workflow
    workflow = MainWorkflow().create_workflow()

    # Get the graph representation
    graph = workflow.get_graph(xray=xray)

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"workflow_graph_{timestamp}.png"
    output_path = os.path.join(output_dir, filename)

    # Ensure the output directory exists
    os.makedirs(output_dir or ".", exist_ok=True)

    # Get the Mermaid syntax
    mermaid_syntax = graph.draw_mermaid()

    # Generate and save the graph visualization using API method with retries
    graph.draw_mermaid_png(
        output_file_path=output_path,
        draw_method=MermaidDrawMethod.API,
        max_retries=5,
        retry_delay=2.0,
    )

    return mermaid_syntax, os.path.abspath(output_path)


if __name__ == "__main__":
    # Default: write to a local `graphs` directory for easier inspection
    default_output_dir = os.path.join(os.getcwd(), "graphs")

    mermaid, png_path = generate_workflow_graph(
        xray=True,
        output_dir=default_output_dir,
    )

    print("Workflow graph generated.")
    print(f"PNG file : {png_path}")
    print("\nMermaid syntax:\n")
    print(mermaid)
