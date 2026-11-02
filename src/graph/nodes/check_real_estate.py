"""
Real-estate check: one small AI call right after the article is fetched, before the
expensive steps, for articles the scheduler found (state.check_real_estate) when
REAL_ESTATE_CHECK_ENABLED is on. An article judged "not real estate" skips every
later step and is reported to the scheduler as such (report_not_real_estate).

- When unsure, the article is kept (the prompt says so): a lost real-estate story
  is worse than an extra article for a reviewer.
- If the check itself fails (model error), the article is kept and processed as before.
- Manual submissions and "process anyway" never set check_real_estate.
"""
import logging
from datetime import datetime, timezone
from pprint import pprint
from typing import Optional

from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from src.configs.settings import settings
from src.models.AgentPromptsModel import AgentPromptsModel
from src.models.MainWorkflowState import MainWorkflowState

logger = logging.getLogger(__name__)

# Built-in prompts; the registry versions (real_estate_check_system/_user, seeded by init_db) win when present.
DEFAULT_SYSTEM = """You decide whether a news article belongs on Propt, a real estate news service for the GCC.

Answer is_real_estate = true when the article is mainly about, or has clear news value for, any of:
- property markets: sales, prices, rents, transactions, market data and forecasts;
- real estate developers, projects, communities, launches and handovers;
- construction of buildings and real estate projects;
- property investment, real estate funds and REITs;
- mortgages and property finance;
- property laws, regulation, ownership rules, and residency or visas linked to property;
- urban planning and infrastructure that shapes where people live and work (new districts, master plans, metro lines);
- hotel, retail, office, industrial and logistics property;
- proptech, architecture and interior design of buildings.

Answer is_real_estate = false when real estate is absent or only a passing mention, for example:
politics, diplomacy, courts and sanctions; sport; crime; health; education; weather; entertainment and culture;
the general economy, banks or stock markets with no property angle; technology with no property angle.

When unsure, answer true: a real estate story wrongly skipped is worse than an extra article for a reviewer.

reason: one short sentence a reviewer can read."""

DEFAULT_USER = """---TITLE---
{title}

---BEGINNING OF THE ARTICLE---
{text}
---END---"""


# Registry entries (init_db.py --mode upsert keeps them equal to this code, like every prompt)
REAL_ESTATE_CHECK_PROMPTS = [
    {"name": "real_estate_check_system", "content": DEFAULT_SYSTEM, "input_variables": [],
     "description": "Real-estate check before processing: what belongs on Propt (keep when unsure)."},
    {"name": "real_estate_check_user", "content": DEFAULT_USER, "input_variables": ["title", "text"],
     "description": "Real-estate check: the article title and the beginning of its text."},
]


class RealEstateCheckModel(BaseModel):
    is_real_estate: bool = Field(..., description="True if the article belongs on a real estate news service; true when unsure")
    reason: str = Field(..., description="One short sentence explaining the decision, for a reviewer")


def get_check_model() -> ChatOpenAI:
    return ChatOpenAI(model=settings.MODEL_NAME, temperature=settings.REAL_ESTATE_CHECK_TEMPERATURE,
                      openai_api_key=settings.OPENAI_API_KEY)


async def run_check(title: str, text: str, prompts: Optional[AgentPromptsModel] = None, config: Optional[dict] = None) -> dict:
    """The check on a title and the beginning of the text. Raises on model errors (callers decide)."""
    system = (prompts.real_estate_check_system if prompts else None) or DEFAULT_SYSTEM
    user = (prompts.real_estate_check_user if prompts else None) or DEFAULT_USER
    message = PromptTemplate.from_template(user).format(title=title or "", text=(text or "")[:settings.REAL_ESTATE_CHECK_CHARS])
    result: RealEstateCheckModel = await get_check_model().with_structured_output(RealEstateCheckModel).ainvoke(
        [("system", system), ("user", message)], config=config or {})
    return {
        "is_real_estate": bool(result.is_real_estate),
        "reason": (result.reason or "").strip()[:500],
        "model": settings.MODEL_NAME,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def check_real_estate(state: MainWorkflowState) -> MainWorkflowState:
    if state.error_message or not state.check_real_estate or not settings.real_estate_check_enabled:
        return state
    title = state.news_article.title if state.news_article else ""
    try:
        check = await run_check(title, state.cleaned_article_text or "", state.active_prompts)
    except Exception as e:
        # Never lose an article because the check failed: process it as before.
        logger.warning(f"[REAL ESTATE CHECK] Failed for {state.source_url}, keeping the article: {e}")
        return state
    pprint(f"[NODE: REAL ESTATE CHECK] {'keep' if check['is_real_estate'] else 'SKIP'}: {check['reason']}")
    return state.model_copy(update={"real_estate_check": check})


def is_skipped(state) -> bool:
    """True when the check judged the article not to be about real estate (state object or graph result dict)."""
    check = state.get("real_estate_check") if isinstance(state, dict) else state.real_estate_check
    error = state.get("error_message") if isinstance(state, dict) else state.error_message
    return not error and isinstance(check, dict) and check.get("is_real_estate") is False


def route_after_check(state: MainWorkflowState) -> str:
    return "skip" if is_skipped(state) else "continue"
