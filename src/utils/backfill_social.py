import traceback
import math
from pprint import pprint
from pymongo import MongoClient
from langchain_core.prompts import PromptTemplate
from src.configs.settings import settings
from src.models.SocialCaptionModel import SocialCaptionModel
from src.db.enums import PromptStatus

def run_social_backfill(limit: int = 0):
    """
    Backfills 'social_media' field for processed articles that are missing it.
    
    Args:
        limit (int): Max number of articles to process. 0 means all.
    """
    print(f"--- 📱 Starting Social Media Caption Backfill (Limit: {limit}) ---")
    
    client = None
    stats = {
        "found": 0,
        "updated": 0,
        "errors": 0,
        "details": []
    }

    try:
        client = MongoClient(settings.DATABASE_URL)
        db = client[settings.MONGO_DB_NAME]
        articles_col = db["processed_articles"]
        prompts_col = db["prompts"]

        # 1. Fetch Prompts
        system_prompt_doc = prompts_col.find_one({"name": "social_caption_system", "status": PromptStatus.ACTIVE})
        user_prompt_doc = prompts_col.find_one({"name": "social_caption_user", "status": PromptStatus.ACTIVE})

        if not system_prompt_doc or not user_prompt_doc:
            raise Exception("Required prompts (social_caption_system/user) not found in DB.")

        system_prompt_template = system_prompt_doc["content"]
        user_prompt_template = user_prompt_doc["content"]

        # 2. Setup LLM
        model = settings.get_model()
        structured_llm = model.with_structured_output(SocialCaptionModel)

        # 3. Find target articles
        # We look for articles that have a summary but NO social_media field
        query = {
            "final_output.summary": {"$exists": True},
            "final_output.social_media": {"$exists": False} 
        }
        
        cursor = articles_col.find(query)
        if limit > 0:
            cursor = cursor.limit(limit)
            
        stats["found"] = articles_col.count_documents(query)
        print(f"--- Found {stats['found']} articles to process ---")

        for doc in cursor:
            try:
                article_id = doc["_id"]
                final_output = doc.get("final_output", {})
                
                title = final_output.get("title", "")
                summary = final_output.get("summary", "")
                
                # Calculate reading time if missing (fallback logic)
                reading_time = final_output.get("reading_time")
                if not reading_time:
                    content_len = len(final_output.get("content", "").split())
                    reading_time = math.ceil(content_len / 200) if content_len > 0 else 2

                if not summary:
                    print(f"  [~] Skipping {article_id}: No summary found.")
                    continue

                # Format prompts
                prompt = PromptTemplate.from_template(user_prompt_template)
                formatted_prompt = prompt.format(
                    title=title,
                    summary=summary,
                    reading_time=reading_time
                )

                messages = [
                    ("system", system_prompt_template),
                    ("user", formatted_prompt)
                ]

                # Invoke LLM
                response: SocialCaptionModel = structured_llm.invoke(messages)
                
                # Convert Pydantic model to Dict for MongoDB storage
                social_data = response.model_dump()

                # Update DB
                articles_col.update_one(
                    {"_id": article_id},
                    {"$set": {"final_output.social_media": social_data}}
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
        raise e
    finally:
        if client:
            client.close()
            
    print(f"--- 🏁 Finished. Updated: {stats['updated']}, Errors: {stats['errors']} ---")
    return stats