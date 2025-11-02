from pydantic import BaseModel
from src.models.ValidationResultModel import ValidationResultModel

class SummaryAttemptModel(BaseModel):
    """
    A model to store a single summary attempt and its
    corresponding validation results.
    """
    summary: str
    validation: ValidationResultModel