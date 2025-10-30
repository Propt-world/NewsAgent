from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel
from src.prompts.ContentExtractorPrompt import ContentExtractor, schema
from src.configs.settings import settings
from langchain_core.prompts import PromptTemplate
from pprint import pprint

def content_extractor(state: MainWorkflowState) -> MainWorkflowState:
    """
    Extract the content from the raw extraction result and update the workflow state.

    - Validates that a `raw_extraction_result` is present.
    - Uses the `ContentExtractorPrompt` to extract the content.
    - Returns a new `MainWorkflowState` instance with `ArticleModel` set.
    """
    pprint("[DEBUG][CONTENT EXTRACTOR] Starting content extraction process...")

    try:
        # Initialize the prompt template
        prompt = PromptTemplate.from_template(ContentExtractor)

        # Get the raw extraction result from the workflow state
        raw_extraction_result = state.raw_extraction_result
        pprint(f"[DEBUG][CONTENT EXTRACTOR] Raw extraction result type: {type(raw_extraction_result)}")

        # Validate that raw extraction result exists
        if not raw_extraction_result:
            pprint("[ERROR] No raw extraction result found in workflow state")
            return state.model_copy(update={"validation_results": "No raw extraction result found"})

        # Format the prompt with the raw content and schema
        formatted_prompt = prompt.format(
            raw_content=raw_extraction_result,
            schema=schema
        )
        pprint(f"[DEBUG][CONTENT EXTRACTOR] Formatted prompt length: {len(formatted_prompt)} characters")

        # Get the configured model with structured output
        model = settings.get_model().with_structured_output(ArticleModel)

        # Generate the response using the model
        pprint("[DEBUG][CONTENT EXTRACTOR] Invoking model to generate structured response...")
        response = model.invoke(formatted_prompt)
        pprint("[DEBUG][CONTENT EXTRACTOR] Model response generated successfully")
        pprint(response)

        # Update the workflow state with the extracted article
        updated_state = state.model_copy(update={
            "news_article": response
        })
        pprint("[DEBUG][CONTENT EXTRACTOR] Workflow state updated successfully")

        return updated_state

    except Exception as e:
        pprint(f"[ERROR][CONTENT EXTRACTOR] Exception occurred during content extraction: {type(e).__name__}: {str(e)}")
        pprint(e)
        return state.model_copy(update={
            "news_article": f"Error extracting content: {e}"
        })