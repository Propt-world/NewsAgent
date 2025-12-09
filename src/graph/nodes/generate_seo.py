import traceback
import json
from datetime import datetime
from pprint import pprint
from src.models.MainWorkflowState import MainWorkflowState
# Import both models
from src.models.SeoMetadataModel import SeoMetadataModel, SeoLLMOutput
from src.configs.settings import settings
from langchain_core.prompts import PromptTemplate

def generate_seo(state: MainWorkflowState) -> MainWorkflowState:
    """
    Generates SEO Metadata (Title, Description, Slug) and constructs
    a valid NewsArticle JSON-LD schema.
    """
    pprint("[NODE: SEO] Generating SEO metadata...")

    try:
        if not state.news_article:
            return state

        # 1. Get Prompts & Model
        prompts = state.active_prompts

        # --- FIX: Use the LLM-specific model (Strict Schema) ---
        model = settings.get_model().with_structured_output(SeoLLMOutput)

        # 2. Format Prompt
        user_prompt_template = PromptTemplate.from_template(prompts.seo_user)
        formatted_prompt = user_prompt_template.format(
            title=state.news_article.title,
            summary=state.news_article.summary,
            content_snippet=state.cleaned_article_text[:1000]
        )

        messages = [
            ("system", prompts.seo_system),
            ("user", formatted_prompt)
        ]

        # 3. Invoke LLM (Returns SeoLLMOutput)
        llm_result: SeoLLMOutput = model.invoke(messages)

        # 4. Construct JSON-LD Programmatically
        # (This block remains exactly the same as your original code)
        pub_date = state.news_article.published_date or datetime.now().isoformat()
        mod_date = datetime.now().isoformat()
        image_url = state.news_article.top_image or ""

        json_ld = {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "mainEntityOfPage": {
                "@type": "WebPage",
                "@id": state.source_url
            },
            "headline": llm_result.meta_title,
            "description": llm_result.meta_description,
            "image": image_url,
            "author": {
                "@type": "Organization",
                "name": "PROPT",
                "url": "https://propt.global/"
            },
            "publisher": {
                "@type": "Organization",
                "name": "PROPT",
                "logo": {
                    "@type": "ImageObject",
                    "url": "https://c.animaapp.com/522lkwAj/img/group-1000008904@2x.png"
                }
            },
            "datePublished": pub_date,
            "dateModified": mod_date
        }

        # 5. --- FIX: Convert to Full Model ---
        # Create the full SeoMetadataModel by unpacking the LLM result
        # and adding the calculated JSON-LD
        final_seo_model = SeoMetadataModel(
            **llm_result.model_dump(),
            json_ld_schema=json_ld
        )

        pprint(f"[NODE: SEO] Generated Slug: {final_seo_model.slug}")

        # 6. Update State
        updated_article = state.news_article.model_copy(update={
            "seo": final_seo_model
        })

        return state.model_copy(update={
            "news_article": updated_article
        })

    except Exception as e:
        pprint(f"[NODE: SEO] Error: {e}")
        traceback.print_exc()
        return state.model_copy(update={
            "error_message": f"SEO generation failed: {e}"
        })