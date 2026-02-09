from typing import List, Dict, Any
from langchain_text_splitters import RecursiveCharacterTextSplitter

class Chunker:
    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200):
        # Using from_tiktoken_encoder for token-based splitting matching OpenAI models
        self.splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            model_name="gpt-4",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap
        )

    def chunk_document(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Splits a normalized document into chunks based on 'content'.
        Returns list of dicts where each dict is a chunk with metadata.
        """
        text = doc.get("content", "")
        if not text:
            return []

        chunks = self.splitter.split_text(text)
        chunked_docs = []
        
        for i, chunk_text in enumerate(chunks):
            # Create a shallow copy of metadata
            chunk_metadata = doc.copy()
            # We don't overwrite 'content' here yet, passing the chunk text separately or 
            # letting the caller handle the final text construction.
            # But to keep interface similar:
            chunk_metadata["content_chunk"] = chunk_text
            chunk_metadata["chunk_index"] = i
            chunked_docs.append(chunk_metadata)
            
        return chunked_docs
