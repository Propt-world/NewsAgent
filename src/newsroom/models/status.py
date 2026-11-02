"""Newsroom health and the problems shown in the backoffice banner."""
from typing import Dict, List, Literal

from pydantic import BaseModel, Field

from src.newsroom.models.common import UtcDatetime


class NewsroomHealthResponse(BaseModel):
    status: str = Field(..., examples=["healthy"])
    service: str = Field(default="newsroom", examples=["newsroom"])
    timestamp: UtcDatetime


class StatusProblem(BaseModel):
    code: str = Field(..., examples=["no_compose_worker"])
    level: Literal["warning", "error"]
    message: str


class NewsroomStatusResponse(BaseModel):
    status: Literal["ok", "warning", "error"]
    checked_at: UtcDatetime
    problems: List[StatusProblem]
    facts: Dict[str, object] = {}
