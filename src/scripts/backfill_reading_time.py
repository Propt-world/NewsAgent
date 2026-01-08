import sys
import os
import math
from pymongo import MongoClient
from pprint import pprint

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.configs.settings import settings

def calculate_wpm(text, wpm=200):
    if not text:
        return None
    word_count = len(text.split())
    return math.ceil(word_count / wpm)

def backfill_reading_time():
    print(f"--- 🔄 Starting Reading Time Backfill ---")
    print(f"Database: {settings.MONGO_DB_NAME}")

    try:
        client = MongoClient(settings.DATABASE_URL)
        db = client[settings.MONGO_DB_NAME]
        articles_col = db["processed_articles"]

        # Find articles that successfully finished (have final_output)
        # and verify structure. We update all of them to be safe, or just missing ones.
        # Let's target ones where 'final_output' exists.
        cursor = articles_col.find({"final_output": {"$ne": None}})
        
        updated_count = 0
        skipped_count = 0

        for doc in cursor:
            article_id = doc["_id"]
            final_output = doc.get("final_output", {})
            
            # Check if likely already has it (optional optimization, but fast enough to re-check)
            if "reading_time" in final_output and final_output["reading_time"] is not None:
                skipped_count += 1
                continue

            # Calculate English
            content_en = final_output.get("cleaned_article_text") or final_output.get("content")
            rt_en = calculate_wpm(content_en)

            # Calculate Arabic
            content_ar = final_output.get("content_ar")
            rt_ar = calculate_wpm(content_ar)

            updates = {}
            if rt_en:
                updates["final_output.reading_time"] = rt_en
            if rt_ar:
                updates["final_output.reading_time_ar"] = rt_ar

            if updates:
                articles_col.update_one({"_id": article_id}, {"$set": updates})
                updated_count += 1
                if updated_count % 10 == 0:
                    print(f"  Processed {updated_count} articles...")
            else:
                skipped_count += 1

        print(f"--- ✅ Backfill Complete ---")
        print(f"Updated: {updated_count}")
        print(f"Skipped: {skipped_count}")

    except Exception as e:
        print(f"[ERROR] Backfill failed: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    backfill_reading_time()
