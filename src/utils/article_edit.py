"""
Editing an article's title, summary and "why this matters" from the backoffice, with the same AI steps that
wrote them the first time (user decision 2026-10-23, option B):

- translate(): the article pipeline's translation prompts (translation_system / translation_user) and output
  model, given only the parts being changed (the other sections are left empty), so only those are translated.
  Nothing is saved: the reviewer checks the Arabic before saving.
- save(): stores the English and the Arabic. An English part that changed needs its Arabic too. When the title
  or the summary changed, the SEO metadata and the social captions are written again by the pipeline's own
  steps (generate_seo, generate_social_media), because both are made from the title and summary; a change to
  "why this matters" alone changes neither.

The web address (slug) is made from the title (title_slug), for a title-only edit (PATCH /articles/{id}/title,
title_fields) and for a full edit when the title changed; otherwise it is kept, so an edit never moves an
article's address for nothing.
"""
import re
from typing import Dict, List, Optional, Tuple

from langchain_core.prompts import PromptTemplate

from src.configs.settings import settings
from src.graph.nodes.generate_seo import generate_seo
from src.graph.nodes.generate_social_media import generate_social_media
from src.graph.nodes.load_agent_configuration import load_agent_configuration
from src.models.ArticleModel import ArticleModel
from src.models.MainWorkflowState import MainWorkflowState
from src.models.TranslationModel import TranslationModel
from src.models.WhyThisMattersModel import WhyThisMattersModel

PARTS = ("title", "summary", "why_this_matters")
ARABIC = {"title": "title_ar", "summary": "summary_ar", "why_this_matters": "why_this_matters_ar"}
LABEL = {"title": "title", "summary": "summary", "why_this_matters": '"why this matters"'}


class EditError(ValueError):
    """The edit cannot be made as sent (shown to the reviewer)."""


class ConfigurationError(RuntimeError):
    """The pipeline's prompts could not be loaded (already logged as a Critical Configuration Error)."""


def title_slug(title: str) -> str:
    """The web address made from a title."""
    clean = re.sub(r"[^a-zA-Z0-9\s-]", "", title.lower())
    return re.sub(r"[-\s]+", "-", clean).strip("-")


def title_fields(title: Optional[str]) -> Dict[str, str]:
    """A title-only edit (PATCH /articles/{id}/title): the new title and the web address made from it.
    No AI step and no Arabic (screens that only change the title)."""
    if not title or not title.strip():
        raise EditError("Title cannot be empty.")
    return {"final_output.title": title.strip(), "final_output.seo.slug": title_slug(title)}


def current_texts(final_output: dict) -> Dict[str, str]:
    """The article's English and Arabic title, summary and why this matters as stored."""
    wtm = final_output.get("why_this_matters") or {}
    return {
        "title": final_output.get("title") or "",
        "summary": final_output.get("summary") or "",
        "why_this_matters": wtm.get("content") or "",
        "title_ar": final_output.get("title_ar") or "",
        "summary_ar": final_output.get("summary_ar") or "",
        "why_this_matters_ar": wtm.get("content_ar") or "",
    }


async def _pipeline_state(doc: dict) -> MainWorkflowState:
    """A workflow state for the stored article with the registry's prompts, as the article pipeline loads them."""
    state = MainWorkflowState(source_url=doc.get("url") or "", cleaned_article_text=(doc.get("final_output") or {}).get("content") or "")
    state = await load_agent_configuration(state)
    if state.error_message or state.active_prompts is None:
        raise ConfigurationError(state.error_message or "Failed to load agent configuration")
    return state


async def translate(doc: dict, parts: Dict[str, str]) -> Dict[str, str]:
    """Arabic for the given English parts (title / summary / why_this_matters), with the pipeline's translation
    prompts and model. Returns {"title_ar": ..., ...} for the parts given only."""
    parts = {k: (v or "").strip() for k, v in parts.items() if k in PARTS and (v or "").strip()}
    if not parts:
        raise EditError("Nothing to translate: send the English title, summary or why this matters")
    state = await _pipeline_state(doc)
    prompts = state.active_prompts
    prompt = PromptTemplate.from_template(prompts.translation_user).format(
        title=parts.get("title", ""),
        summary=parts.get("summary", ""),
        why_this_matters=parts.get("why_this_matters", ""),
        content="",  # the article text is not being changed
    )
    model = settings.get_model().with_structured_output(TranslationModel)
    result: TranslationModel = await model.ainvoke([("system", prompts.translation_system), ("user", prompt)])
    answers = {"title": result.title_ar, "summary": result.summary_ar, "why_this_matters": result.why_this_matters_ar}
    missing = [LABEL[k] for k in parts if not (answers.get(k) or "").strip()]
    if missing:
        raise EditError(f"The translation came back without the Arabic {', '.join(missing)}; try again")
    return {ARABIC[k]: answers[k].strip() for k in parts}


async def save(doc: dict, edit: Dict[str, Optional[str]]) -> Tuple[Dict[str, object], List[str]]:
    """
    The fields to $set on the stored article for this edit, and what was written again ("seo", "social_media").
    edit: any of title, summary, why_this_matters and their *_ar; a field left out (None) is not changed.
    """
    final_output = doc.get("final_output") or {}
    now = current_texts(final_output)
    new = dict(now)
    for key, value in edit.items():
        if key in now and value is not None:
            new[key] = value.strip()

    if not new["title"]:
        raise EditError("The title cannot be empty")
    changed = [k for k in PARTS if new[k] != now[k]]
    for key in changed:
        if not new[ARABIC[key]]:
            raise EditError(f"Add the Arabic {LABEL[key]} too (use Translate to Arabic, or type it)")
        if new[ARABIC[key]] == now[ARABIC[key]]:
            raise EditError(f"The English {LABEL[key]} changed but its Arabic did not: translate it or update it")
    changed_ar = [ARABIC[k] for k in PARTS if new[ARABIC[k]] != now[ARABIC[k]]]
    if not changed and not changed_ar:
        raise EditError("Nothing changed")

    fields: Dict[str, object] = {}
    if "title" in changed or "title_ar" in changed_ar:
        fields.update({"final_output.title": new["title"], "final_output.title_ar": new["title_ar"]})
    if "summary" in changed or "summary_ar" in changed_ar:
        fields.update({"final_output.summary": new["summary"], "final_output.summary_ar": new["summary_ar"]})
    if "why_this_matters" in changed or "why_this_matters_ar" in changed_ar:
        wtm = final_output.get("why_this_matters") or {}
        fields["final_output.why_this_matters"] = WhyThisMattersModel(
            **{**wtm, "content": new["why_this_matters"], "content_ar": new["why_this_matters_ar"]}).model_dump(mode="json")

    regenerated: List[str] = []
    if "title" in changed or "summary" in changed:
        # The SEO metadata and the social captions are written from the title and summary: write them again
        state = await _pipeline_state(doc)
        # Only what the two steps read: older stored articles do not always match today's article model
        article = ArticleModel(
            title=new["title"], summary=new["summary"], content=final_output.get("content") or "",
            published_date=final_output.get("published_date"), top_image=final_output.get("top_image"),
            reading_time=final_output.get("reading_time") if isinstance(final_output.get("reading_time"), int) else None)
        state = state.model_copy(update={"news_article": article})
        old_seo = final_output.get("seo") or {}
        state = await generate_seo(state)
        state = await generate_social_media(state)
        written = state.news_article
        if written.seo is not None:  # None: the step failed (logged inside) and the old SEO stays
            seo = written.seo.model_dump(mode="json")
            seo["slug"] = title_slug(new["title"]) if "title" in changed else old_seo.get("slug") or seo.get("slug")
            fields["final_output.seo"] = seo
            regenerated.append("seo")
        if written.social_media is not None:
            fields["final_output.social_media"] = written.social_media.model_dump(mode="json")
            regenerated.append("social_media")
        if "title" in changed and "seo" not in regenerated and final_output.get("seo"):
            # SEO could not be written again: the address still follows the new title, as a title edit does
            fields["final_output.seo.slug"] = title_slug(new["title"])
    return fields, regenerated
