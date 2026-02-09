import logging
from typing import List, Dict, Any, Optional
from pymongo import AsyncMongoClient
import hashlib
from datetime import datetime, timezone
from openai import AsyncOpenAI
from src.configs.settings import settings
import certifi
import asyncio
import ormsgpack

logger = logging.getLogger(__name__)


# Singleton instance maintenance
_mongo_client_instance = None
_openai_client_instance = None

class MongoStore:
    def __init__(self):
        """Initialize MongoDB connection with proper SSL/TLS configuration."""
        global _mongo_client_instance, _openai_client_instance
        
        # Reuse existing client if available
        if _mongo_client_instance:
            self.client = _mongo_client_instance
        else:
            connection_params = {
                "connectTimeoutMS": 30000,
                "socketTimeoutMS": 30000,
                "serverSelectionTimeoutMS": 30000,
                "retryWrites": True,
                "retryReads": True,
                # Connection pool settings
                "maxPoolSize": 100,
                "minPoolSize": 0,
                "maxIdleTimeMS": 50000,
                "waitQueueTimeoutMS": 2000
            }
            
            # Configure TLS/SSL for MongoDB Atlas
            if settings.DATABASE_URL.startswith("mongodb://") and not settings.DATABASE_URL.startswith("mongodb+srv://"):
                connection_params["tls"] = True
                try:
                    connection_params["tlsCAFile"] = certifi.where()
                except Exception as e:
                    logger.warning(f"Could not load certifi certificates: {e}. Using system certificates.")
            
            logger.info("Initializing new Async MongoDB client...")
            self.client = AsyncMongoClient(settings.DATABASE_URL, **connection_params)
            _mongo_client_instance = self.client
            
            # We cannot ping in __init__ because it's async-incompatible without event loop running here
            # Connection verification will happen on first query or explicit warmup

        self.db = self.client[settings.MONGO_DB_NAME]
        self.collection = self.db[settings.MONGO_DB_COLLECTION]
        self.bio_collection = self.db[settings.MONGO_DB_BIO_COLLECTION]
        
        if _openai_client_instance:
             self.openai_client = _openai_client_instance
        else:
             self.openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
             _openai_client_instance = self.openai_client

    async def get_embedding(self, text: str) -> List[float]:
        """Generates embedding for the given text."""
        text = text.replace("\n", " ")
        response = await self.openai_client.embeddings.create(
            input=[text], 
            model=settings.MONGO_EMBEDDING_MODEL
        )
        return response.data[0].embedding

    def _generate_content_hash(self, source_id: str, chunk_text: str) -> str:
        """Generates a deterministic hash for idempotency."""
        raw = f"{source_id}:{chunk_text}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def add_documents(self, documents: List[Dict[str, Any]]):
        """
        Adds documents to MongoDB.
        Expects documents to have 'chunk_text', 'source_id', and 'metadata'.
        """
        for doc in documents:
            try:
                chunk_text = doc.get("chunk_text")
                source_id = doc.get("source_id")
                
                if not chunk_text:
                    logger.warning(f"Skipping document with empty chunk_text: {source_id}")
                    continue

                content_hash = self._generate_content_hash(source_id, chunk_text)
                
                # Check for existence (Idempotency)
                if await self.collection.find_one({"_id": content_hash}):
                    logger.debug(f"Document {content_hash} already exists. Skipping.")
                    continue

                # Generate Embedding
                embedding = await self.get_embedding(chunk_text)
                
                # Construct MongoDB Document
                mongo_doc = {
                    "_id": content_hash,
                    "source_id": source_id,
                    "chunk_text": chunk_text,
                    "embedding": embedding,
                    "metadata": doc.get("metadata", {}),
                    "created_at": datetime.now(timezone.utc)
                }
                
                await self.collection.insert_one(mongo_doc)
                logger.info(f"Inserted document {content_hash} for source {source_id}")
                
            except Exception as e:
                logger.error(f"Error processing document {doc.get('source_id')}: {e}")

    async def query(self, query_text: str, limit: int = 5, filters: Optional[Dict] = None) -> List[Dict]:
        """
        Performs Vector Search using the specific index.
        """
        query_embedding = await self.get_embedding(query_text)
        
        pipeline = [
            {
                "$vectorSearch": {
                    "index": settings.MONGO_VECTOR_INDEX_NAME,
                    "path": "embedding",
                    "queryVector": query_embedding,
                    "numCandidates": limit * 10, # heuristics for ANN
                    "limit": limit
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "chunk_text": 1,
                    "source_id": 1,
                    "metadata": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]

        if filters:
             pipeline[0]["$vectorSearch"]["filter"] = filters

        # Async aggregation returns a coroutine that yields a cursor
        cursor = await self.collection.aggregate(pipeline)
        results = await cursor.to_list(length=limit)
        return results

    def create_vector_index(self):
        """
        Prints the instructions to create the index.
        """
        index_def = {
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": 3072,
                    "similarity": "cosine"
                },
                {
                    "type": "filter",
                    "path": "metadata.category"
                },
                {
                    "type": "filter",
                    "path": "metadata.published_date"
                }
            ]
        }
        
        logger.info("To create the vector index in MongoDB Atlas:")
        logger.info(f"1. Go to Database > Atlas Search > Create Search Index")
        logger.info(f"2. Select 'JSON Editor'")
        logger.info(f"3. Database: {settings.MONGO_DB_NAME}, Collection: {settings.MONGO_DB_COLLECTION}")
        logger.info(f"4. Index Name: {settings.MONGO_VECTOR_INDEX_NAME}")
        logger.info("5. Usage this configuration:")
        logger.info(str(index_def))

    async def get_chatbot_bio(self, default_bio: str = "You are a helpful and accurate assistant.") -> str:
        """
        Retrieves the chatbot bio from MongoDB.
        """
        try:
            bio_doc = await self.bio_collection.find_one({"type": "main_bio"})
            if bio_doc:
                return bio_doc.get("bio", default_bio)
            return default_bio
        except Exception as e:
            logger.error(f"Error fetching chatbot bio: {e}")
            return default_bio

    async def save_chatbot_bio(self, bio: str):
        """
        Saves or updates the chatbot bio in MongoDB.
        """
        try:
            await self.bio_collection.update_one(
                {"type": "main_bio"},
                {
                    "$set": {
                        "bio": bio,
                        "updated_at": datetime.now(timezone.utc)
                    }
                },
                upsert=True
            )
            logger.info("Successfully updated chatbot bio in MongoDB")
        except Exception as e:
            logger.error(f"Error saving chatbot bio: {e}")
            raise e

    async def list_sessions(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists sessions with their first message and metadata, newest first."""
        try:
            checkpoint_collection = self.db["checkpoints"]
            
            if session_id:
                encoded_session_id = ormsgpack.packb(session_id)
                query = {"metadata.session_id": ["msgpack", encoded_session_id]}
                thread_ids = await checkpoint_collection.distinct("thread_id", query)
            else:
                thread_ids = await checkpoint_collection.distinct("thread_id")
            
            # Remove null/empty
            thread_ids = [tid for tid in thread_ids if tid]
            
            # Initialize graph once for all sessions
            app_graph = None
            try:
                from src.graph.graph import MainWorkflow
                workflow = MainWorkflow()
                app_graph = workflow.create_workflow()
            except Exception as e:
                logger.error(f"Error initializing graph in list_sessions: {e}")
                # Fallback to simple listing if graph fails
                return [{"thread_id": tid, "first_message": "Memory Unavailable", "timestamp": None} for tid in thread_ids[-50:]]

            # Fetch details for threads in parallel
            async def get_thread_info(tid):
                try:
                    messages = await self.get_session_messages(tid, app_graph=app_graph)
                    first_msg = "New Conversation"
                    timestamp = None
                    
                    if messages:
                        for msg in messages:
                            if msg["role"] == "user":
                                first_msg = msg["content"]
                                timestamp = msg.get("timestamp")
                                break
                    return {
                        "thread_id": tid,
                        "first_message": first_msg,
                        "timestamp": timestamp
                    }
                except Exception as e:
                    logger.error(f"Error fetching info for thread {tid}: {e}")
                    return None

            # Get latest 50 sessions to avoid massive overhead
            target_ids = thread_ids[-50:]
            tasks = [get_thread_info(tid) for tid in target_ids]
            results = await asyncio.gather(*tasks)
            
            sessions = [r for r in results if r is not None]
            
            # Sort by timestamp (newest first)
            sessions.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
            return sessions
        except Exception as e:
            logger.error(f"Error listing sessions: {e}")
            return []

    async def get_session_messages(self, thread_id: str, app_graph=None) -> List[Dict[str, Any]]:
        """
        Retrieves messages for a given thread_id with timestamps.
        """
        if app_graph is None:
            from src.graph.graph import MainWorkflow
            workflow = MainWorkflow()
            app_graph = workflow.create_workflow()
        
        config = {"configurable": {"thread_id": thread_id}}
        try:
            state = await app_graph.aget_state(config)
        except Exception as e:
            logger.error(f"Error getting state for thread {thread_id}: {e}")
            return []
        
        messages = []
        if state and state.values:
            raw_messages = state.values.get("messages", [])
            for msg in raw_messages:
                role = "user" if msg.type == "human" else "assistant"
                # Extract timestamp from additional_kwargs
                timestamp = getattr(msg, "additional_kwargs", {}).get("timestamp")
                
                messages.append({
                    "role": role,
                    "content": msg.content,
                    "timestamp": timestamp,
                    "citations": getattr(msg, "additional_kwargs", {}).get("citations", [])
                })
        return messages

    def close(self):
        # Do not close the shared client here, as it may be used by others.
        # Connections are managed by the connection pool.
        pass

def get_mongo_store():
    return MongoStore()
