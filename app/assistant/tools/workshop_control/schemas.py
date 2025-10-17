"""
Schemas for Workshop Control Tools

Defines Pydantic models for tool parameters and responses.
"""

from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import Optional, Dict, Any, List
from datetime import datetime


class WorkshopControlParams(BaseModel):
    """Base parameters for workshop control tools"""
    workshop_id: int = Field(..., ge=1, description="ID of the workshop")
    user_id: int = Field(..., ge=1, description="ID of the user executing the command")


class BeginWorkshopParams(WorkshopControlParams):
    """Parameters for beginning a workshop"""
    pass


class NextTaskParams(WorkshopControlParams):
    """Parameters for advancing to next task"""
    pass


class EndCurrentTaskParams(WorkshopControlParams):
    """Parameters for ending current task"""
    pass


class PauseWorkshopParams(WorkshopControlParams):
    """Parameters for pausing workshop"""
    pass


class ResumeWorkshopParams(WorkshopControlParams):
    """Parameters for resuming workshop"""
    pass


class StopWorkshopParams(WorkshopControlParams):
    """Parameters for stopping workshop"""
    pass


class AddIdeaParams(WorkshopControlParams):
    """Parameters for adding an idea"""
    text: str = Field(..., min_length=1, max_length=1000, description="The idea text")
    
    @field_validator('text')
    @classmethod
    def validate_text(cls, v: str) -> str:
        """Validate and clean idea text"""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Idea text cannot be empty or whitespace only")
        return cleaned


class CastVoteParams(WorkshopControlParams):
    """Parameters for casting a vote"""
    cluster_id: int = Field(..., ge=1, description="ID of the cluster to vote for")


class AddCommentParams(WorkshopControlParams):
    """Parameters for adding a comment"""
    text: str = Field(..., min_length=1, max_length=2000, description="The comment text")
    parent_type: str = Field(..., description="Type of parent entity (idea, cluster, discussion)")
    parent_id: int = Field(..., ge=1, description="ID of parent entity")
    
    @field_validator('text')
    @classmethod
    def validate_text(cls, v: str) -> str:
        """Validate and clean comment text"""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Comment text cannot be empty or whitespace only")
        return cleaned
    
    @field_validator('parent_type')
    @classmethod
    def validate_parent_type(cls, v: str) -> str:
        """Validate parent type"""
        allowed = ['idea', 'cluster', 'discussion']
        if v not in allowed:
            raise ValueError(f"parent_type must be one of {allowed}")
        return v


class TaskInfo(BaseModel):
    """Information about a workshop task"""
    id: int
    title: str
    description: Optional[str] = None
    sequence: int
    duration_minutes: Optional[int] = None
    status: str


class WorkshopControlResponse(BaseModel):
    """Standard response for workshop control actions"""
    success: bool
    message: str
    workshop_status: Optional[str] = None
    current_task: Optional[TaskInfo] = None
    previous_task: Optional[str] = None
    completed: bool = False
    data: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "message": "Workshop started successfully",
                "workshop_status": "inprogress",
                "current_task": {
                    "id": 1,
                    "title": "Workshop Briefing",
                    "description": "Frame the session with objectives",
                    "sequence": 1,
                    "duration_minutes": 10,
                    "status": "running",
                },
                "completed": False,
            }
        }
    )
