"""ponytail: temporary one-off script, delete after use."""
import os
import random

from pymongo import MongoClient


def main():
    client = MongoClient(os.environ["DATABASE_URL"])
    sources = client[os.environ["MONGO_DB_NAME"]]["sources"]

    ids = [doc["_id"] for doc in sources.find({}, {"_id": 1})]
    if not ids:
        print("No sources found.")
        return

    keep_id = random.choice(ids)
    sources.update_many({"_id": {"$ne": keep_id}}, {"$set": {"is_active": False}})
    sources.update_one({"_id": keep_id}, {"$set": {"is_active": True}})
    print(f"Kept active: {keep_id}. Deactivated {len(ids) - 1} others.")


if __name__ == "__main__":
    main()
