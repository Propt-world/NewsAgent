from pydantic import BaseModel, Field
from typing import Any, Optional, Dict, List

# --- GENERIC RESPONSES ---
class GenericResponse(BaseModel):
    status: str = Field(..., example="success")
    message: str = Field(..., example="Operation completed successfully")
    id: Optional[str] = Field(None, description="Resource ID, if applicable")

class HealthResponse(BaseModel):
    status: str = Field(..., example="healthy")
    redis: str = Field(..., example="connected")
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
    source_url: str
    created_at: str
    result: Optional[Dict[str, Any]] = Field(None, description="The final processed article data")
    error: Optional[str] = None

# --- QUEUE RELATED ---
class QueueInfo(BaseModel):
    name: str
    count: int

class QueueStatusResponse(BaseModel):
    status: str
    main_queue: QueueInfo
    dead_letter_queue: QueueInfo