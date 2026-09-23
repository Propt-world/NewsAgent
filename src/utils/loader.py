import json
from typing import List, Dict, Any


def clean_text(text: str) -> str:
    """
    Basic text normalization.
    """
    if not text:
        return ""
    # Remove excessive whitespace
    text = " ".join(text.split())
    return text

def normalize_document(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Standardizes document structure for ingestion.
    Extracts data from 'final_output' if present.
    """
    # Check if we have the nested 'final_output' structure
    data_source = doc.get("final_output", doc)
    
    content = data_source.get("content") or data_source.get("body") or ""
    title = data_source.get("title") or "Untitled"
    summary = data_source.get("summary") or ""
    
    # URL might be at top level or inside final_output, usually top level in this dataset
    url = doc.get("url") or data_source.get("url") or ""
    
    # Date handling
    date = doc.get("published_date") or data_source.get("published_date") or ""
    
    # Handle mongo object ID
    source_id = str(doc.get("source_id") or doc.get("_id", ""))

    return {
        "source_id": source_id,
        "content": clean_text(content),
        "title": clean_text(title),
        "summary": clean_text(summary),
        "url": url,
        "published_date": str(date),
        # Preserve original fields for metadata if needed
        "original_metadata": {
             k: v for k, v in data_source.items() 
             if k not in ["content", "title", "summary"] and isinstance(v, (str, int, float, bool, list))
        }
    }
