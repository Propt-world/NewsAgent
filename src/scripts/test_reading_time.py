import sys
import os

# Setup path to import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from src.models.MainWorkflowState import MainWorkflowState
from src.models.ArticleModel import ArticleModel
from src.graph.nodes.calculate_reading_time import calculate_reading_time

def test_reading_time_calculation():
    print("--- Testing Calculate Reading Time Node ---")
    
    # 1. Create Mock State
    # 200 words exactly -> 1 min
    # 201 words -> 2 min
    
    english_text_200 = "word " * 200
    english_text_201 = "word " * 201
    
    arabic_text_50 = "كلمة " * 50
    arabic_text_401 = "كلمة " * 401
    
    article = ArticleModel(
        title="Test Article",
        content=english_text_200,
        content_ar=arabic_text_50
    )
    
    state = MainWorkflowState(
        source_url="http://test.com",
        cleaned_article_text=english_text_201, # Should be used for English calculation
        news_article=article
    )
    
    # 2. Run Node
    new_state = calculate_reading_time(state)
    
    # 3. Verify Results
    rt_en = new_state.news_article.reading_time
    rt_ar = new_state.news_article.reading_time_ar
    
    print(f"English Reading Time (Expect 2): {rt_en}")
    print(f"Arabic Reading Time (Expect 1): {rt_ar}")
    
    assert rt_en == 2, f"Expected 2 min for English, got {rt_en}"
    assert rt_ar == 1, f"Expected 1 min for Arabic, got {rt_ar}"
    
    print("--- Test Passed ---")

if __name__ == "__main__":
    test_reading_time_calculation()
