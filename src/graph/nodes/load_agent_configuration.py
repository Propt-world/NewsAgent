import traceback
from pprint import pprint
from pymongo import MongoClient
from src.db.enums import PromptStatus
from src.models.MainWorkflowState import MainWorkflowState
from src.models.AgentPromptsModel import AgentPromptsModel
from src.configs.settings import settings

def load_agent_configuration(state: MainWorkflowState) -> MainWorkflowState:
    """
    Node: LOAD AGENT CONFIGURATION

    Responsibilities:
    1. Connects to MongoDB.
    2. Fetches the 'ACTIVE' version of every prompt required by the system.
    3. Validates that no required prompts are missing using AgentPromptsModel.
    4. Populates 'state.active_prompts' so downstream nodes can use them.
    """
    pprint("[NODE: LOAD CONFIG] Starting configuration load...")

    # The list of logical names the system expects.
    # These must match the fields in src/models/AgentPromptsModel.py
    REQUIRED_PROMPTS = [
        "content_extractor",
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
        "translation_user"
        ]

    try:
        # 1. Establish Database Connection
        client = MongoClient(settings.DATABASE_URL)
        db = client[settings.MONGO_DB_NAME]
        collection = db["prompts"]

        # 2. Query for Active Prompts
        # fetch all prompts where name is in REQUIRED_PROMPTS and status is ACTIVE
        results = collection.find({
            "name": {"$in": REQUIRED_PROMPTS},
            "status": PromptStatus.ACTIVE
        })

        # 3. Convert List of Docs to Dictionary
        raw_prompts_dict = {}
        for doc in results:
            raw_prompts_dict[doc["name"]] = doc["content"]

        client.close()

        # 4. Strict Validation (The "Guard Rail")
        # By trying to instantiate the Pydantic model, we automatically check:
        # - Are all required fields present?
        # - Are they strings?
        pprint(f"[NODE: LOAD CONFIG] Found {len(raw_prompts_dict)} active prompts. Validating...")

        prompts_model = AgentPromptsModel(**raw_prompts_dict)

        pprint("[NODE: LOAD CONFIG] Configuration validated successfully.")

        # 5. Update State
        return state.model_copy(update={
            "active_prompts": prompts_model
        })

    except Exception as e:
        pprint(f"[NODE: LOAD CONFIG] Critical Configuration Error: {e}")
        traceback.print_exc()

        # We return the error state so the graph can handle it gracefully (or stop)
        return state.model_copy(update={
            "error_message": f"Failed to load agent configuration: {str(e)}"
        })