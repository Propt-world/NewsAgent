import traceback

from langchain_core.prompts import PromptTemplate

from src.configs.settings import settings
from src.db.connections import get_async_mongo_database, get_legacy_mongo_database
from src.db.enums import PromptStatus
from src.models.CountryExtractionModel import CountryExtractionModel


def _initial_stats() -> dict:
    return {
        "found": 0,
        "updated": 0,
        "errors": 0,
        "details": [],
    }


def _country_backfill_query() -> dict:
    return {
        "final_output.summary": {"$exists": True},
        "final_output.countries": {"$exists": False},
    }


def _format_country_messages(
    system_prompt_template: str,
    user_prompt_template: str,
    final_output: dict,
):
    prompt = PromptTemplate.from_template(user_prompt_template)
    formatted_prompt = prompt.format(
        title=final_output.get("title", ""),
        summary=final_output.get("summary", ""),
        content=(final_output.get("content", "") or "")[:1000],
    )

    return [
        ("system", system_prompt_template),
        ("user", formatted_prompt),
    ]


async def run_country_backfill_async(limit: int = 0):
    """
    Backfills countries for processed articles using async Mongo and async LLM calls.

    The response shape intentionally matches the legacy sync function:
    found, updated, errors, and details.
    """
    print(f"--- Starting Country Extraction Backfill (Limit: {limit}) ---")

    stats = _initial_stats()

    try:
        db = get_async_mongo_database()
        articles_col = db["processed_articles"]
        prompts_col = db["prompts"]

        system_prompt_doc = await prompts_col.find_one(
            {"name": "country_extraction_system", "status": PromptStatus.ACTIVE}
        )
        user_prompt_doc = await prompts_col.find_one(
            {"name": "country_extraction_user", "status": PromptStatus.ACTIVE}
        )

        if not system_prompt_doc or not user_prompt_doc:
            raise Exception("Required prompts not found in DB.")

        structured_llm = settings.get_model().with_structured_output(
            CountryExtractionModel
        )
        query = _country_backfill_query()
        cursor = articles_col.find(query)
        if limit > 0:
            cursor = cursor.limit(limit)

        stats["found"] = await articles_col.count_documents(query)

        async for doc in cursor:
            try:
                article_id = doc["_id"]
                final_output = doc.get("final_output", {})

                if not final_output.get("summary"):
                    continue

                messages = _format_country_messages(
                    system_prompt_doc["content"],
                    user_prompt_doc["content"],
                    final_output,
                )

                response: CountryExtractionModel = await structured_llm.ainvoke(
                    messages
                )
                countries = response.countries

                await articles_col.update_one(
                    {"_id": article_id},
                    {"$set": {"final_output.countries": countries}},
                )

                stats["updated"] += 1
                stats["details"].append(f"Updated {article_id}: {countries}")
                print(f"  [+] Updated {article_id}: {countries}")

            except Exception as e:
                stats["errors"] += 1
                error_msg = f"Error processing article {doc.get('_id')}: {str(e)}"
                stats["details"].append(error_msg)
                print(f"  [!] {error_msg}")

    except Exception as e:
        print(f"[FATAL ERROR] Backfill failed: {e}")
        traceback.print_exc()
        raise

    return stats


def run_country_backfill(limit: int = 0):
    """
    Legacy sync country backfill entrypoint.

    Kept for scripts or emergency use while the API moves to
    run_country_backfill_async().
    """
    print(f"--- Starting Legacy Country Extraction Backfill (Limit: {limit}) ---")

    stats = _initial_stats()

    try:
        db = get_legacy_mongo_database()
        articles_col = db["processed_articles"]
        prompts_col = db["prompts"]

        system_prompt_doc = prompts_col.find_one(
            {"name": "country_extraction_system", "status": PromptStatus.ACTIVE}
        )
        user_prompt_doc = prompts_col.find_one(
            {"name": "country_extraction_user", "status": PromptStatus.ACTIVE}
        )

        if not system_prompt_doc or not user_prompt_doc:
            raise Exception("Required prompts not found in DB.")

        structured_llm = settings.get_model().with_structured_output(
            CountryExtractionModel
        )
        query = _country_backfill_query()
        cursor = articles_col.find(query)
        if limit > 0:
            cursor = cursor.limit(limit)

        stats["found"] = articles_col.count_documents(query)

        for doc in cursor:
            try:
                article_id = doc["_id"]
                final_output = doc.get("final_output", {})

                if not final_output.get("summary"):
                    continue

                messages = _format_country_messages(
                    system_prompt_doc["content"],
                    user_prompt_doc["content"],
                    final_output,
                )

                response: CountryExtractionModel = structured_llm.invoke(messages)
                countries = response.countries

                articles_col.update_one(
                    {"_id": article_id},
                    {"$set": {"final_output.countries": countries}},
                )

                stats["updated"] += 1
                stats["details"].append(f"Updated {article_id}: {countries}")
                print(f"  [+] Updated {article_id}: {countries}")

            except Exception as e:
                stats["errors"] += 1
                error_msg = f"Error processing article {doc.get('_id')}: {str(e)}"
                stats["details"].append(error_msg)
                print(f"  [!] {error_msg}")

    except Exception as e:
        print(f"[FATAL ERROR] Legacy backfill failed: {e}")
        traceback.print_exc()
        raise

    return stats
