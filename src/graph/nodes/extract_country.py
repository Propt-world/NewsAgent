import traceback
from pprint import pprint
from langchain_core.prompts import PromptTemplate
from src.models.MainWorkflowState import MainWorkflowState
from src.models.CountryExtractionModel import CountryExtractionModel
from src.configs.settings import settings

def extract_country(state: MainWorkflowState) -> MainWorkflowState:
    """
    Extracts the country or countries relevant to the article.
    """
    pprint("[NODE: EXTRACT COUNTRY] Starting country extraction...")

    # --- 0. FAIL FAST CHECK ---
    if state.error_message:
        return state

    try:
        # 1. Guards: Check if we have content
        if not state.news_article or not state.news_article.summary:
            pprint("[NODE: EXTRACT COUNTRY] No article/summary. Skipping.")
            return state.model_copy(update={
                "error_message": "No article/summary found for country extraction."
            })

        prompts = state.active_prompts

        # 2. Get LLM with structured output
        model = settings.get_model()
        structured_llm = model.with_structured_output(CountryExtractionModel)

        # 3. Format the prompt
        prompt = PromptTemplate.from_template(prompts.country_extraction_user)
        formatted_prompt = prompt.format(
            title=state.news_article.title,
            summary=state.news_article.summary,
            content=state.cleaned_article_text[:1000] # Pass first 1000 chars of content for context
        )

        messages = [
            ("system", prompts.country_extraction_system),
            ("user", formatted_prompt)
        ]

        pprint("[NODE: EXTRACT COUNTRY] Invoking LLM...")

        # 4. Call the LLM
        response: CountryExtractionModel = structured_llm.invoke(messages)
        
        extracted_countries = response.countries
        pprint(f"[NODE: EXTRACT COUNTRY] Extracted countries: {extracted_countries}")

        # 5. Update State
        updated_article = state.news_article.model_copy(update={
            "countries": extracted_countries
        })

        return state.model_copy(update={"news_article": updated_article})

    except Exception as e:
        pprint(f"[NODE: EXTRACT COUNTRY] Error: {e}")
        traceback.print_exc()
        # Create a fallback/error state, or just continue without countries
        # Continuing without crashing is usually better for metadata extraction
        return state.model_copy(update={"error_message": f"Error in extract_country: {e}"})
