import math
import traceback

from langchain_core.prompts import PromptTemplate

from src.configs.settings import settings
from src.db.connections import get_async_mongo_database, get_legacy_mongo_database
from src.db.enums import PromptStatus
from src.models.SocialCaptionModel import SocialCaptionModel


def _initial_stats() -> dict:
    return {
        "found": 0,
        "updated": 0,
        "errors": 0,
        "details": [],
    }


def _social_backfill_query() -> dict:
    return {
        "final_output.summary": {"$exists": True},
        "final_output.social_media": {"$exists": False},
    }


def _format_social_messages(
    system_prompt_template: str,
    user_prompt_template: str,
    final_output: dict,
):
    reading_time = final_output.get("reading_time")
    if not reading_time:
        content_len = len((final_output.get("content", "") or "").split())
        reading_time = math.ceil(content_len / 200) if content_len > 0 else 2

    prompt = PromptTemplate.from_template(user_prompt_template)
    formatted_prompt = prompt.format(
        title=final_output.get("title", ""),
        summary=final_output.get("summary", ""),
        reading_time=reading_time,
    )

    return [
        ("system", system_prompt_template),
        ("user", formatted_prompt),
    ]


async def run_social_backfill_async(limit: int = 0):
    """
    Backfills social captions using async Mongo and async LLM calls.

    The response shape intentionally matches the legacy sync function:
    found, updated, errors, and details.
    """
    print(f"--- Starting Social Media Caption Backfill (Limit: {limit}) ---")

    stats = _initial_stats()

    try:
        db = get_async_mongo_database()
        articles_col = db["processed_articles"]
        prompts_col = db["prompts"]

        system_prompt_doc = await prompts_col.find_one(
            {"name": "social_caption_system", "status": PromptStatus.ACTIVE}
        )
        user_prompt_doc = await prompts_col.find_one(
            {"name": "social_caption_user", "status": PromptStatus.ACTIVE}
        )

        if not system_prompt_doc or not user_prompt_doc:
            raise Exception("Required prompts (social_caption_system/user) not found.")

        structured_llm = settings.get_model().with_structured_output(
            SocialCaptionModel
        )
        query = _social_backfill_query()
        cursor = articles_col.find(query)
        if limit > 0:
            cursor = cursor.limit(limit)

        stats["found"] = await articles_col.count_documents(query)
        print(f"--- Found {stats['found']} articles to process ---")

        async for doc in cursor:
            try:
                article_id = doc["_id"]
                final_output = doc.get("final_output", {})

                if not final_output.get("summary"):
                    print(f"  [~] Skipping {article_id}: No summary found.")
                    continue

                messages = _format_social_messages(
                    system_prompt_doc["content"],
                    user_prompt_doc["content"],
                    final_output,
                )

                response: SocialCaptionModel = await structured_llm.ainvoke(messages)
                social_data = response.model_dump()

                await articles_col.update_one(
                    {"_id": article_id},
                    {"$set": {"final_output.social_media": social_data}},
                )

                stats["updated"] += 1
                print(f"  [+] Updated {article_id}: {response.headline}")

            except Exception as e:
                stats["errors"] += 1
                error_msg = f"Error processing article {doc.get('_id')}: {str(e)}"
                stats["details"].append(error_msg)
                print(f"  [!] {error_msg}")

    except Exception as e:
        print(f"[FATAL ERROR] Backfill failed: {e}")
        traceback.print_exc()
        raise

    print(f"--- Finished. Updated: {stats['updated']}, Errors: {stats['errors']} ---")
    return stats


def run_social_backfill(limit: int = 0):
    """
    Legacy sync social-caption backfill entrypoint.

    Kept for scripts or emergency use while the API moves to
    run_social_backfill_async().
    """
    print(f"--- Starting Legacy Social Media Caption Backfill (Limit: {limit}) ---")

    stats = _initial_stats()

    try:
        db = get_legacy_mongo_database()
        articles_col = db["processed_articles"]
        prompts_col = db["prompts"]

        system_prompt_doc = prompts_col.find_one(
            {"name": "social_caption_system", "status": PromptStatus.ACTIVE}
        )
        user_prompt_doc = prompts_col.find_one(
            {"name": "social_caption_user", "status": PromptStatus.ACTIVE}
        )

        if not system_prompt_doc or not user_prompt_doc:
            raise Exception("Required prompts (social_caption_system/user) not found.")

        structured_llm = settings.get_model().with_structured_output(
            SocialCaptionModel
        )
        query = _social_backfill_query()
        cursor = articles_col.find(query)
        if limit > 0:
            cursor = cursor.limit(limit)

        stats["found"] = articles_col.count_documents(query)
        print(f"--- Found {stats['found']} articles to process ---")

        for doc in cursor:
            try:
                article_id = doc["_id"]
                final_output = doc.get("final_output", {})

                if not final_output.get("summary"):
                    print(f"  [~] Skipping {article_id}: No summary found.")
                    continue

                messages = _format_social_messages(
                    system_prompt_doc["content"],
                    user_prompt_doc["content"],
                    final_output,
                )

                response: SocialCaptionModel = structured_llm.invoke(messages)
                social_data = response.model_dump()

                articles_col.update_one(
                    {"_id": article_id},
                    {"$set": {"final_output.social_media": social_data}},
                )

                stats["updated"] += 1
                print(f"  [+] Updated {article_id}: {response.headline}")

            except Exception as e:
                stats["errors"] += 1
                error_msg = f"Error processing article {doc.get('_id')}: {str(e)}"
                stats["details"].append(error_msg)
                print(f"  [!] {error_msg}")

    except Exception as e:
        print(f"[FATAL ERROR] Legacy backfill failed: {e}")
        traceback.print_exc()
        raise

    print(f"--- Finished. Updated: {stats['updated']}, Errors: {stats['errors']} ---")
    return stats
