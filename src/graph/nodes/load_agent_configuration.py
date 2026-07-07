import inspect
import logging
import os
import traceback
from pprint import pprint

import certifi
from pymongo import AsyncMongoClient

from src.configs.settings import settings
from src.db.enums import PromptStatus
from src.models.AgentPromptsModel import AgentPromptsModel
from src.models.MainWorkflowState import MainWorkflowState


logger = logging.getLogger(__name__)
_async_config_client = None

REQUIRED_PROMPTS = [
    "summary_system",
    "summary_initial_user",
    "summary_retry_user",
    "validation_system",
    "validation_user",
    "relevance_system",
    "relevance_user",
    "search_system",
    "search_user",
    "categorization_system",
    "categorization_user",
    "seo_system",
    "seo_user",
    "translation_system",
    "translation_user",
    "country_extraction_system",
    "country_extraction_user",
    "content_enrichment_system",
    "content_enrichment_user",
    "social_caption_system",
    "social_caption_user",
]


def _mongo_client_options() -> dict:
    options = {
        "connectTimeoutMS": int(os.getenv("MONGO_CONNECT_TIMEOUT_MS", "5000")),
        "socketTimeoutMS": int(os.getenv("MONGO_SOCKET_TIMEOUT_MS", "5000")),
        "serverSelectionTimeoutMS": int(
            os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000")
        ),
        "retryWrites": True,
        "retryReads": True,
        "maxPoolSize": int(os.getenv("MONGO_MAX_POOL_SIZE", "100")),
        "minPoolSize": int(os.getenv("MONGO_MIN_POOL_SIZE", "0")),
        "maxIdleTimeMS": int(os.getenv("MONGO_MAX_IDLE_TIME_MS", "50000")),
        "waitQueueTimeoutMS": int(os.getenv("MONGO_WAIT_QUEUE_TIMEOUT_MS", "2000")),
    }

    if "mongodb.net" in settings.DATABASE_URL or settings.DATABASE_URL.startswith(
        "mongodb+srv://"
    ):
        options["tls"] = True
        options["tlsCAFile"] = certifi.where()

    return options


def get_async_config_database():
    global _async_config_client

    if _async_config_client is None:
        logger.info("Initializing async graph configuration MongoDB client.")
        _async_config_client = AsyncMongoClient(
            settings.DATABASE_URL,
            **_mongo_client_options(),
        )

    return _async_config_client[settings.MONGO_DB_NAME]


async def close_async_config_client() -> None:
    global _async_config_client

    if _async_config_client is None:
        return

    result = _async_config_client.close()
    if inspect.isawaitable(result):
        await result
    _async_config_client = None


async def load_agent_configuration(state: MainWorkflowState) -> MainWorkflowState:
    """
    Node: LOAD AGENT CONFIGURATION

    Responsibilities:
    1. Connects to MongoDB.
    2. Fetches the active version of every prompt required by the system.
    3. Fetches the category mapping (Name -> External ID).
    4. Validates that no required prompts are missing.
    5. Populates state.active_prompts and state.category_mapping.
    """
    pprint("[NODE: LOAD CONFIG] Starting configuration load...")

    try:
        db = get_async_config_database()

        prompts_cursor = db["prompts"].find(
            {
                "name": {"$in": REQUIRED_PROMPTS},
                "status": PromptStatus.ACTIVE,
            }
        )

        raw_prompts_dict = {}
        async for doc in prompts_cursor:
            raw_prompts_dict[doc["name"]] = doc["content"]

        cat_cursor = db["categories"].find({}, {"name": 1, "external_id": 1})

        cat_map = {}
        async for doc in cat_cursor:
            if doc.get("name") and doc.get("external_id"):
                cat_map[doc["name"]] = doc["external_id"]

        pprint(f"[NODE: LOAD CONFIG] Loaded {len(cat_map)} category mappings.")
        pprint(
            f"[NODE: LOAD CONFIG] Found {len(raw_prompts_dict)} active prompts. Validating..."
        )

        prompts_model = AgentPromptsModel(**raw_prompts_dict)

        pprint("[NODE: LOAD CONFIG] Configuration validated successfully.")

        return state.model_copy(
            update={
                "active_prompts": prompts_model,
                "category_mapping": cat_map,
            }
        )

    except Exception as e:
        pprint(f"[NODE: LOAD CONFIG] Critical Configuration Error: {e}")
        traceback.print_exc()

        return state.model_copy(
            update={
                "error_message": f"Failed to load agent configuration: {str(e)}"
            }
        )
