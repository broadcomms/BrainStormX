from __future__ import annotations

from typing import Optional

from . import ToolRegistry
from .database import AddIdeaTool, CastVoteTool, CreateActionItemTool
from .notifier.service import NotifierService
from .time import (
    GetCurrentTimeTool,
    GetPhaseTimingTool,
    QueryRecentActivityTool,
    ScheduleReminderTool,
    StartTimerTool,
)
from .workshop_control import (
    BeginWorkshopTool,
    NextTaskTool,
    EndCurrentTaskTool,
    PauseWorkshopTool,
    ResumeWorkshopTool,
    StopWorkshopTool,
    AddIdeaTool as WorkshopAddIdeaTool,
    CastVoteTool as WorkshopCastVoteTool,
)
from .workshop import VoteForClusterTool
from .workshop_data import GetAgendaTool, ListClustersTool, ListReportsTool
from .workshop_data.list_ideas import ListIdeasTool
from .workshop_data.read_report import ReadReportTool


def build_default_registry(*, include_notifier: bool = True) -> ToolRegistry:
    """Construct the default tool registry used by the assistant."""
    registry = ToolRegistry()
    
    # Legacy database tools (kept for backward compatibility)
    registry.register(AddIdeaTool())
    registry.register(CastVoteTool())
    registry.register(CreateActionItemTool())
    
    # Workshop control tools (organizer actions)
    registry.register(BeginWorkshopTool())
    registry.register(NextTaskTool())
    registry.register(EndCurrentTaskTool())
    registry.register(PauseWorkshopTool())
    registry.register(ResumeWorkshopTool())
    registry.register(StopWorkshopTool())
    
    # Workshop control tools (participant actions)
    # Note: Using aliased imports to distinguish from legacy tools
    registry.register(WorkshopAddIdeaTool())
    registry.register(WorkshopCastVoteTool())
    # Note: AddCommentTool removed - not yet implemented (placeholder only)
    
    # Notifier and time tools
    if include_notifier:
        registry.register(NotifierService())
    registry.register(GetCurrentTimeTool())
    registry.register(GetPhaseTimingTool())
    registry.register(QueryRecentActivityTool())
    registry.register(StartTimerTool())
    registry.register(ScheduleReminderTool())
    
    # Workshop data lookup tools
    registry.register(VoteForClusterTool())
    registry.register(GetAgendaTool())
    registry.register(ListClustersTool())
    registry.register(ListIdeasTool())
    registry.register(ListReportsTool())
    registry.register(ReadReportTool())
    
    return registry
