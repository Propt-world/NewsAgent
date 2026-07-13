from pydantic import BaseModel, Field
from typing import Any, Optional, Dict, List
from datetime import datetime

# --- GENERIC RESPONSES ---
class GenericResponse(BaseModel):
    status: str = Field(..., example="success")
    message: str = Field(..., example="Operation completed successfully")
    id: Optional[str] = Field(None, description="Resource ID, if applicable")

class HealthResponse(BaseModel):
    status: str = Field(..., example="healthy")
    queue: str = Field(..., example="connected")
    browserless: str = Field(..., example="connected")
    scheduler: str = Field(..., example="reachable")
    graph_logic: str = Field(..., example="operational")

# --- JOB RELATED ---
class JobSubmissionResponse(BaseModel):
    job_id: str = Field(..., description="The unique identifier for the submitted job")
    status: str = Field(..., example="queued")
    queue_position: int = Field(..., description="Current position in the processing queue")
    message: str = Field(..., example="Job successfully queued")

class JobStatusResponse(BaseModel):
    job_id: str
    status: str = Field(..., example="processing", description="Current state: queued, processing, completed, failed")
    source_url: Optional[str] = None
    created_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = Field(None, description="The final processed article data")
    error: Optional[str] = None

# --- QUEUE RELATED ---
class DLQCountResponse(BaseModel):
    dlq_count: int

class QueueInfo(BaseModel):
    name: str
    count: int

class QueueStatusResponse(BaseModel):
    status: str
    main_queue: QueueInfo
    dead_letter_queue: QueueInfo

class SchedulerStandardHealthResponse(BaseModel):
    status: str = Field(..., example="healthy")
    service: str = Field(default="scheduler", example="scheduler")
    scheduler: str = Field(..., example="running")
    database: str = Field(..., example="last_known_connected")
    database_checked_at: Optional[datetime] = None
    timestamp: datetime

class SchedulerDatabaseHealthResponse(BaseModel):
    status: str = Field(..., example="healthy")
    service: str = Field(default="scheduler", example="scheduler")
    database: str = Field(..., example="connected")
    timestamp: datetime

class SchedulerStandardHealthResponse(BaseModel):
    status: str = Field(..., example="healthy")
    service: str = Field(default="scheduler", example="scheduler")
    scheduler: str = Field(..., example="running")
    database: str = Field(..., example="last_known_connected")
    database_checked_at: Optional[datetime] = None
    timestamp: datetime

class SchedulerDatabaseHealthResponse(BaseModel):
    status: str = Field(..., example="healthy")
    service: str = Field(default="scheduler", example="scheduler")
    database: str = Field(..., example="connected")
    timestamp: datetime

class SchedulerHealthResponse(BaseModel):
    status: str = Field(..., example="healthy")
    service: str = Field(default="scheduler", example="scheduler")
    database: str = Field(..., example="connected")
    redis: str = Field(..., example="connected")
    browserless: str = Field(..., example="connected")
    scheduler: str = Field(..., example="running")
    main_api: str = Field(..., example="reachable")
    timestamp: datetime
