"""
Organizer Control Tools

Tools for workshop organizers to control workshop flow via natural language.
"""

import uuid
import logging
from typing import Dict, Any

from app.assistant.tools.base import ToolSchema, ToolResult
from app.models import Workshop, User, BrainstormTask, db
# NOTE: Import advance_to_next_task at runtime to avoid circular import
# from app.workshop.advance import advance_to_next_task
from .base import WorkshopControlTool
from .schemas import (
    BeginWorkshopParams,
    NextTaskParams,
    EndCurrentTaskParams,
    PauseWorkshopParams,
    ResumeWorkshopParams,
    StopWorkshopParams,
    WorkshopControlResponse
)

logger = logging.getLogger(__name__)


class BeginWorkshopTool(WorkshopControlTool):
    """
    Tool to begin a workshop by advancing to the first task.
    
    Only organizers can execute this action.
    Validates workshop state and delegates to existing advance logic.
    """
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = True
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="begin_workshop",
            namespace="workshop_control",
            description="Begin the workshop by advancing to the first task. Only organizers can execute this command.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {
                        "type": "integer",
                        "description": "ID of the workshop to begin",
                        "minimum": 1
                    },
                    "user_id": {
                        "type": "integer",
                        "description": "ID of the user executing the command",
                        "minimum": 1
                    }
                },
                "required": ["workshop_id", "user_id"],
                "additionalProperties": False
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "current_task": {"type": "object"},
                    "workshop_status": {"type": "string"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "begin the workshop"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """Execute workshop begin with state validation"""
        
        # Validate workshop is not completed
        valid, error = self._validate_workshop_not_completed(workshop)
        if not valid:
            return ToolResult(
                success=False,
                error=error,
                correlation_id=correlation_id
            )
        
        # Validate workshop is not cancelled
        valid, error = self._validate_workshop_not_cancelled(workshop)
        if not valid:
            return ToolResult(
                success=False,
                error=error,
                correlation_id=correlation_id
            )
        
        # Treat repeated begin calls during active execution as idempotent
        if workshop.status == "inprogress" and workshop.current_task_id is not None:
            current_task = db.session.get(BrainstormTask, workshop.current_task_id)
            if current_task and current_task.status == "running":
                # Treat as idempotent success so repeated "start" commands don't surface errors or narration
                return ToolResult(
                    success=True,
                    data={
                        "message": "Workshop already in progress",
                        "current_task": self._get_task_info(current_task),
                        "workshop_status": workshop.status,
                        "completed": False
                    },
                    rows_affected=0,
                    correlation_id=correlation_id,
                    metadata={"suppress_narration": True}
                )
        
        # Handle resume scenario when workshop is paused
        if workshop.status == "paused":
            from datetime import datetime
            resumed_task = None
            if workshop.current_task_id:
                task = db.session.get(BrainstormTask, workshop.current_task_id)
                if task:
                    resumed_task = self._get_task_info(task)

            # Resume timer accounting: preserve elapsed-before-pause and start a new run window
            workshop.status = "inprogress"
            workshop.timer_start_time = datetime.utcnow()
            workshop.timer_paused_at = None
            db.session.commit()

            try:
                from flask_socketio import emit
                # Also emit a fresh timer_sync so clients unpause without waiting a heartbeat
                try:
                    from app.sockets_core.core import emit_timer_sync  # type: ignore
                    room = f"workshop_room_{workshop.id}"
                    remaining = 0
                    try:
                        remaining = int(workshop.get_remaining_task_time())
                    except Exception:
                        remaining = 0
                    emit_timer_sync(room, {
                        "task_id": workshop.current_task_id,
                        "remaining_seconds": remaining,
                        "is_paused": False,
                    }, workshop_id=workshop.id)
                except Exception as _e:
                    logger.warning(f"Failed to emit timer_sync on resume from begin tool: {_e}")
                emit('workshop_resumed', {
                    'workshop_id': workshop.id
                }, room=f'workshop_{workshop.id}', namespace='/')
            except Exception as e:
                logger.warning(f"Failed to emit workshop_resumed event from begin tool: {e}")

            return ToolResult(
                success=True,
                data={
                    "message": "Workshop resumed from pause",
                    "current_task": resumed_task,
                    "workshop_status": workshop.status,
                    "completed": False
                },
                rows_affected=1,
                correlation_id=correlation_id,
                metadata={"suppress_narration": True, "resume": True}
            )

        # Set workshop status to inprogress
        workshop.status = "inprogress"
        db.session.commit()
        
        # Delegate to existing advance logic (import at runtime to avoid circular import)
        from app.workshop.advance import advance_to_next_task
        success, result = advance_to_next_task(workshop.id)
        
        if not success:
            return ToolResult(
                success=False,
                error=f"Failed to start first task: {result}",
                correlation_id=correlation_id
            )
        
        # Get the started task for response
        current_task = None
        if workshop.current_task_id:
            task = db.session.get(BrainstormTask, workshop.current_task_id)
            if task:
                current_task = self._get_task_info(task)
        
        # Success!
        return ToolResult(
            success=True,
            data={
                "message": "Workshop started successfully",
                "current_task": current_task,
                "workshop_status": workshop.status,
                "completed": False
            },
            rows_affected=1,
            correlation_id=correlation_id,
            metadata={"suppress_narration": True}
        )


class NextTaskTool(WorkshopControlTool):
    """
    Tool to advance to the next task in the workshop sequence.
    
    Ends the current task and starts the next one.
    Only organizers can execute this action.
    """
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = True
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="next_task",
            namespace="workshop_control",
            description="Advance to the next task in the workshop. Ends current task and starts the next one. Only organizers can execute this.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "user_id": {"type": "integer", "minimum": 1}
                },
                "required": ["workshop_id", "user_id"]
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "previous_task": {"type": "string"},
                    "current_task": {"type": "object"},
                    "completed": {"type": "boolean"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "advance to next task"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """Advance to next task"""
        
        # Validate workshop is in progress
        valid, error = self._validate_workshop_in_progress(workshop)
        if not valid:
            return ToolResult(
                success=False,
                error=error,
                correlation_id=correlation_id
            )
        
        # Validate has current task
        valid, error = self._validate_has_current_task(workshop)
        if not valid:
            return ToolResult(
                success=False,
                error=error,
                correlation_id=correlation_id
            )
        
        # Get current task info for response
        previous_task_title = None
        if workshop.current_task_id:
            prev_task = db.session.get(BrainstormTask, workshop.current_task_id)
            if prev_task:
                previous_task_title = prev_task.title
        
        # Delegate to existing advance logic (import at runtime to avoid circular import)
        from app.workshop.advance import advance_to_next_task
        success, result = advance_to_next_task(workshop.id)
        
        if not success:
            # Check if we've reached the end
            if "No more tasks" in str(result) or "completed" in str(result).lower():
                return ToolResult(
                    success=True,
                    data={
                        "message": "Workshop completed - no more tasks",
                        "previous_task": previous_task_title,
                        "current_task": None,
                        "workshop_status": "completed",
                        "completed": True
                    },
                    correlation_id=correlation_id
                )
            else:
                return ToolResult(
                    success=False,
                    error=f"Failed to advance to next task: {result}",
                    correlation_id=correlation_id
                )
        
        # Get new current task
        current_task = None
        if workshop.current_task_id:
            task = db.session.get(BrainstormTask, workshop.current_task_id)
            if task:
                current_task = self._get_task_info(task)
        
        return ToolResult(
            success=True,
            data={
                "message": f"Advanced to next task",
                "previous_task": previous_task_title,
                "current_task": current_task,
                "workshop_status": workshop.status,
                "completed": False
            },
            rows_affected=1,
            correlation_id=correlation_id,
            metadata={"suppress_narration": True}
        )


class EndCurrentTaskTool(WorkshopControlTool):
    """
    Tool to end the current task without advancing to the next one.
    
    Stops the timer and marks current task as completed.
    Only organizers can execute this action.
    """
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = True
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="end_current_task",
            namespace="workshop_control",
            description="End the current task and stop the timer. Does not advance to next task. Only organizers can execute this.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "user_id": {"type": "integer", "minimum": 1}
                },
                "required": ["workshop_id", "user_id"]
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "ended_task": {"type": "object"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "end the current task"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """End current task"""
        from datetime import datetime
        
        # Validate workshop is in progress
        valid, error = self._validate_workshop_in_progress(workshop)
        if not valid:
            return ToolResult(
                success=False,
                error=error,
                correlation_id=correlation_id
            )
        
        # Validate has current task
        valid, error = self._validate_has_current_task(workshop)
        if not valid:
            return ToolResult(
                success=False,
                error=error,
                correlation_id=correlation_id
            )
        
        # Get current task
        current_task = db.session.get(BrainstormTask, workshop.current_task_id)
        if not current_task:
            return ToolResult(
                success=False,
                error="Current task not found",
                correlation_id=correlation_id
            )
        
        # End the task
        current_task.status = "completed"
        current_task.ended_at = datetime.utcnow()
        
        # Get task info for response
        task_info = self._get_task_info(current_task)
        
        # Commit changes
        db.session.commit()
        
        # Emit Socket.IO event for real-time UI update
        try:
            from flask_socketio import emit
            emit('task_ended', {
                'workshop_id': workshop.id,
                'task_id': current_task.id,
                'task_title': current_task.title
            }, room=f'workshop_{workshop.id}', namespace='/')
        except Exception as e:
            logger.warning(f"Failed to emit task_ended event: {e}")
        
        return ToolResult(
            success=True,
            data={
                "message": f"Task '{current_task.title}' ended successfully",
                "ended_task": task_info,
                "workshop_status": workshop.status
            },
            rows_affected=1,
            correlation_id=correlation_id
        )


class PauseWorkshopTool(WorkshopControlTool):
    """Tool to pause the workshop and timer"""
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = True
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="pause_workshop",
            namespace="workshop_control",
            description="Pause the workshop and timer. Only organizers can execute this.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "user_id": {"type": "integer", "minimum": 1}
                },
                "required": ["workshop_id", "user_id"]
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "workshop_status": {"type": "string"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "pause the workshop"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """Pause workshop"""
        
        # Validate workshop is in progress
        if workshop.status != "inprogress":
            return ToolResult(
                success=False,
                error=f"Can only pause a workshop in progress (current status: {workshop.status})",
                correlation_id=correlation_id
            )
        
        # Freeze timer accounting
        from datetime import datetime
        elapsed_this_run = 0
        try:
            if workshop.timer_start_time:
                elapsed_this_run = int(max((datetime.utcnow() - workshop.timer_start_time).total_seconds(), 0))
        except Exception:
            elapsed_this_run = 0
        try:
            workshop.timer_elapsed_before_pause = int((workshop.timer_elapsed_before_pause or 0) + max(elapsed_this_run, 0))
        except Exception:
            # Ensure it's an int
            workshop.timer_elapsed_before_pause = int(max(elapsed_this_run, 0))

        workshop.status = "paused"
        workshop.timer_paused_at = datetime.utcnow()
        db.session.commit()
        
        # Emit Socket.IO event
        try:
            from flask_socketio import emit
            emit('workshop_paused', {
                'workshop_id': workshop.id
            }, room=f'workshop_{workshop.id}', namespace='/')
            # Also emit immediate timer_sync snapshot so clients freeze without waiting
            try:
                from app.sockets_core.core import emit_timer_sync  # type: ignore
                room = f"workshop_room_{workshop.id}"
                remaining = 0
                try:
                    remaining = int(workshop.get_remaining_task_time())
                except Exception:
                    remaining = 0
                emit_timer_sync(room, {
                    "task_id": workshop.current_task_id,
                    "remaining_seconds": remaining,
                    "is_paused": True,
                }, workshop_id=workshop.id)
            except Exception as _e:
                logger.warning(f"Failed to emit timer_sync on pause: {_e}")
        except Exception as e:
            logger.warning(f"Failed to emit workshop_paused event: {e}")
        
        return ToolResult(
            success=True,
            data={
                "message": "Workshop paused successfully",
                "workshop_status": workshop.status
            },
            rows_affected=1,
            correlation_id=correlation_id
        )


class ResumeWorkshopTool(WorkshopControlTool):
    """Tool to resume a paused workshop"""
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = True
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="resume_workshop",
            namespace="workshop_control",
            description="Resume a paused workshop. Only organizers can execute this.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "user_id": {"type": "integer", "minimum": 1}
                },
                "required": ["workshop_id", "user_id"]
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "workshop_status": {"type": "string"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "resume the workshop"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """Resume workshop"""
        
        # Validate workshop is paused
        if workshop.status != "paused":
            return ToolResult(
                success=False,
                error=f"Can only resume a paused workshop (current status: {workshop.status})",
                correlation_id=correlation_id
            )
        
        # Resume timer: keep elapsed-before-pause, clear paused_at, and start a new run window
        from datetime import datetime
        workshop.status = "inprogress"
        workshop.timer_start_time = datetime.utcnow()
        workshop.timer_paused_at = None
        db.session.commit()
        
        # Emit Socket.IO event
        try:
            from flask_socketio import emit
            emit('workshop_resumed', {
                'workshop_id': workshop.id
            }, room=f'workshop_{workshop.id}', namespace='/')
            # Emit fresh timer_sync so clients restart countdown correctly
            try:
                from app.sockets_core.core import emit_timer_sync  # type: ignore
                room = f"workshop_room_{workshop.id}"
                remaining = 0
                try:
                    remaining = int(workshop.get_remaining_task_time())
                except Exception:
                    remaining = 0
                emit_timer_sync(room, {
                    "task_id": workshop.current_task_id,
                    "remaining_seconds": remaining,
                    "is_paused": False,
                }, workshop_id=workshop.id)
            except Exception as _e:
                logger.warning(f"Failed to emit timer_sync on resume: {_e}")
        except Exception as e:
            logger.warning(f"Failed to emit workshop_resumed event: {e}")
        
        return ToolResult(
            success=True,
            data={
                "message": "Workshop resumed successfully",
                "workshop_status": workshop.status
            },
            rows_affected=1,
            correlation_id=correlation_id
        )


class StopWorkshopTool(WorkshopControlTool):
    """Tool to stop the workshop completely"""
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = True
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="stop_workshop",
            namespace="workshop_control",
            description="Stop the workshop completely and mark it as completed. Only organizers can execute this.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "user_id": {"type": "integer", "minimum": 1}
                },
                "required": ["workshop_id", "user_id"]
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "workshop_status": {"type": "string"},
                    "completed": {"type": "boolean"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "stop the workshop"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """Stop workshop"""
        from datetime import datetime
        
        # Validate workshop is not already completed
        if workshop.status == "completed":
            return ToolResult(
                success=False,
                error="Workshop is already completed",
                correlation_id=correlation_id
            )
        
        # End current task if running
        if workshop.current_task_id:
            current_task = db.session.get(BrainstormTask, workshop.current_task_id)
            if current_task and current_task.status == "running":
                current_task.status = "completed"
                current_task.ended_at = datetime.utcnow()
        
        # Set workshop to completed
        workshop.status = "completed"
        db.session.commit()
        
        # Emit Socket.IO event
        try:
            from flask_socketio import emit
            emit('workshop_stopped', {
                'workshop_id': workshop.id
            }, room=f'workshop_{workshop.id}', namespace='/')
        except Exception as e:
            logger.warning(f"Failed to emit workshop_stopped event: {e}")
        
        return ToolResult(
            success=True,
            data={
                "message": "Workshop stopped and marked as completed",
                "workshop_status": workshop.status,
                "completed": True
            },
            rows_affected=1,
            correlation_id=correlation_id
        )
