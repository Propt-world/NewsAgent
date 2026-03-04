import sys
import os
sys.path.append(os.getcwd())

from src.utils.chunker import Chunker
from src.utils.loader import normalize_document
import json

def test_embedding_logic():
    print("Testing Embedding Utilities...")
    
    # Mock Article Data
    mock_article = {
        "_id": "test_id_123",
        "url": "http://example.com",
        "final_output": {
            "title": "Test Article Title",
            "summary": "This is a summary.",
            "content": "This is the main content of the article. " * 50, # repeatable content
            "url": "http://example.com/canonical",
            "published_date": "2023-01-01"
        }
    }
    
    # 1. Test Normalization
    print("\n1. Testing Normalization...")
    norm_doc = normalize_document(mock_article)
    print(f"Normalized Keys: {norm_doc.keys()}")
    assert norm_doc["title"] == "Test Article Title"
    assert "source_id" in norm_doc
    print("Normalization Passed.")

    # 2. Test Chunking
    print("\n2. Testing Chunking...")
    chunker = Chunker(chunk_size=100, chunk_overlap=20)
    chunks = chunker.chunk_document(norm_doc)
    print(f"Generated {len(chunks)} chunks.")
    
    if chunks:
        print("Sample Chunk 0 Metadata:")
        print(json.dumps({k:v for k,v in chunks[0].items() if k != 'content_chunk'}, indent=2))
        print(f"Chunk 0 Text Length: {len(chunks[0]['content_chunk'])}")
    
    assert len(chunks) > 0
    print("Chunking Passed.")

if __name__ == "__main__":
    test_embedding_logic()
