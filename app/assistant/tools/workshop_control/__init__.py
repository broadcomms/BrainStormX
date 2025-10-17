"""
Workshop Control Tools Module

Provides voice-controlled orchestration tools for workshop management.
Organizers can control workshop flow (begin, next, pause, resume, stop)
and participants can take actions (add ideas, vote, comment) via natural language.
"""

from .organizer_tools import (
    BeginWorkshopTool,
    NextTaskTool,
    EndCurrentTaskTool,
    PauseWorkshopTool,
    ResumeWorkshopTool,
    StopWorkshopTool
)

from .participant_tools import (
    AddIdeaTool,
    CastVoteTool,
    AddCommentTool
)

__all__ = [
    # Organizer tools
    "BeginWorkshopTool",
    "NextTaskTool",
    "EndCurrentTaskTool",
    "PauseWorkshopTool",
    "ResumeWorkshopTool",
    "StopWorkshopTool",
    # Participant tools
    "AddIdeaTool",
    "CastVoteTool",
    "AddCommentTool",
]
