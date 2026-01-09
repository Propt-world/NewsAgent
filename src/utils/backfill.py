import traceback
from pymongo import MongoClient
from langchain_core.prompts import PromptTemplate
from src.configs.settings import settings
from src.models.CountryExtractionModel import CountryExtractionModel
from src.db.enums import PromptStatus

def run_country_backfill(limit: int = 0):
    """
    Backfills 'countries' field for processed articles that are missing it.
    
    Args:
        limit (int): Max number of articles to process. 0 means all.
        
    Returns:
        dict: Statistics about the operation (updated, errors, total_found).
    """
    print(f"--- 🔄 Starting Country Extraction Backfill (Limit: {limit}) ---")
    
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
        system_prompt_doc = prompts_col.find_one({"name": "country_extraction_system", "status": PromptStatus.ACTIVE})
        user_prompt_doc = prompts_col.find_one({"name": "country_extraction_user", "status": PromptStatus.ACTIVE})

        if not system_prompt_doc or not user_prompt_doc:
            raise Exception("Required prompts not found in DB.")

        system_prompt_template = system_prompt_doc["content"]
        user_prompt_template = user_prompt_doc["content"]

        # 2. Setup LLM
        model = settings.get_model()
        structured_llm = model.with_structured_output(CountryExtractionModel)

        # 3. Find target articles
        query = {
            "final_output.summary": {"$exists": True},
            "final_output.countries": {"$exists": False} 
        }
        
        cursor = articles_col.find(query)
        if limit > 0:
            cursor = cursor.limit(limit)
            
        # Count might be inaccurate if limit is applied, but gives an idea of total available
        stats["found"] = articles_col.count_documents(query) 

        for doc in cursor:
            try:
                article_id = doc["_id"]
                final_output = doc.get("final_output", {})
                
                title = final_output.get("title", "")
                summary = final_output.get("summary", "")
                content = final_output.get("content", "")[:1000]

                if not summary:
                    continue

                # Format prompts
                prompt = PromptTemplate.from_template(user_prompt_template)
                formatted_prompt = prompt.format(
                    title=title,
                    summary=summary,
                    content=content
                )

                messages = [
                    ("system", system_prompt_template),
                    ("user", formatted_prompt)
                ]

                # Invoke LLM
                response: CountryExtractionModel = structured_llm.invoke(messages)
                countries = response.countries

                # Update DB
                articles_col.update_one(
                    {"_id": article_id},
                    {"$set": {"final_output.countries": countries}}
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
        raise e
    finally:
        if client:
            client.close()
            
    return stats
