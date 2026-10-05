from datetime import datetime

from pydantic import BaseModel, Field


class NewsroomHealthResponse(BaseModel):
    status: str = Field(..., examples=["healthy"])
    service: str = Field(default="newsroom", examples=["newsroom"])
    timestamp: datetime
