"""
Base classes for Workshop Control Tools

Provides shared RBAC validation, state checking, and audit logging.
"""

import uuid
import logging
from typing import Dict, Any, Optional, Tuple
from abc import abstractmethod
from datetime import datetime

from app.assistant.tools.base import BaseTool, ToolSchema, ToolResult
from app.models import Workshop, User, BrainstormTask, db

logger = logging.getLogger(__name__)


class WorkshopControlTool(BaseTool):
    """
    Base class for all workshop control tools.
    
    Provides:
    - RBAC validation (organizer/participant checks)
    - Workshop state validation
    - Audit logging
    - Consistent error handling
    """
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = False  # Override in subclasses
    
    def execute(self, params: Dict[str, Any]) -> ToolResult:
        """
        Execute with RBAC and state validation.
        
        This method enforces the security boundary before delegating
        to the subclass-specific _execute_action method.
        """
        correlation_id = str(uuid.uuid4())
        
        try:
            # Extract common parameters
            workshop_id = params.get("workshop_id")
            user_id = params.get("user_id")
            
            if not workshop_id or not user_id:
                return ToolResult(
                    success=False,
                    error="Missing required parameters: workshop_id and user_id",
                    correlation_id=correlation_id
                )
            
            # Fetch entities
            workshop = db.session.get(Workshop, workshop_id)
            if not workshop:
                return ToolResult(
                    success=False,
                    error=f"Workshop with ID {workshop_id} not found",
                    correlation_id=correlation_id
                )
            
            user = db.session.get(User, user_id)
            if not user:
                return ToolResult(
                    success=False,
                    error=f"User with ID {user_id} not found",
                    correlation_id=correlation_id
                )
            
            # RBAC validation
            is_organizer = self._is_organizer(workshop, user)
            
            if self.requires_organizer and not is_organizer:
                self._log_rbac_denial(workshop_id, user_id, "organizer_required")
                return ToolResult(
                    success=False,
                    error=f"Only the workshop organizer can {self._get_action_description()}",
                    correlation_id=correlation_id
                )
            
            # Log the action attempt
            self._log_action_attempt(workshop_id, user_id, params)
            
            # Delegate to subclass implementation
            result = self._execute_action(workshop, user, params, correlation_id)
            
            # Log the result
            if result.success:
                self._log_action_success(workshop_id, user_id, result)
            else:
                self._log_action_failure(workshop_id, user_id, result)
            
            return result
            
        except Exception as e:
            logger.exception(f"Unexpected error in {self.__class__.__name__}")
            return ToolResult(
                success=False,
                error=f"An unexpected error occurred: {str(e)}",
                correlation_id=correlation_id
            )
    
    @abstractmethod
    def _execute_action(
        self, 
        workshop: Workshop, 
        user: User, 
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """
        Execute the specific action. Must be implemented by subclasses.
        
        Args:
            workshop: The workshop entity (already validated)
            user: The user entity (already validated)
            params: Tool parameters
            correlation_id: Unique ID for this operation
            
        Returns:
            ToolResult with success/failure and data
        """
        pass
    
    @abstractmethod
    def _get_action_description(self) -> str:
        """
        Return a human-readable description of the action for error messages.
        
        Example: "begin the workshop", "advance to next task"
        """
        pass
    
    # RBAC Helpers
    
    def _is_organizer(self, workshop: Workshop, user: User) -> bool:
        """Check if user is the workshop organizer"""
        return workshop.created_by_id == user.user_id
    
    def _is_participant(self, workshop: Workshop, user: User) -> bool:
        """
        Check if user is a participant.
        
        Note: In current implementation, anyone who is not the organizer
        is considered a participant. Future enhancement could add explicit
        participant roster validation.
        """
        # TODO: Check against explicit participant list when implemented
        return True
    
    # State Validation Helpers
    
    def _validate_workshop_not_completed(self, workshop: Workshop) -> Tuple[bool, Optional[str]]:
        """Validate workshop is not in completed status"""
        if workshop.status == "completed":
            return False, "Cannot modify a completed workshop"
        return True, None
    
    def _validate_workshop_not_cancelled(self, workshop: Workshop) -> Tuple[bool, Optional[str]]:
        """Validate workshop is not cancelled"""
        if workshop.status == "cancelled":
            return False, "Cannot modify a cancelled workshop"
        return True, None
    
    def _validate_workshop_in_progress(self, workshop: Workshop) -> Tuple[bool, Optional[str]]:
        """Validate workshop is currently in progress"""
        if workshop.status != "inprogress":
            return False, f"Workshop must be in progress (current status: {workshop.status})"
        return True, None
    
    def _validate_has_current_task(self, workshop: Workshop) -> Tuple[bool, Optional[str]]:
        """Validate workshop has a current task running"""
        if not workshop.current_task_id:
            return False, "No task is currently running"
        
        current_task = db.session.get(BrainstormTask, workshop.current_task_id)
        if not current_task:
            return False, "Current task not found"
        
        return True, None
    
    # Audit Logging
    
    def _log_action_attempt(self, workshop_id: int, user_id: int, params: Dict[str, Any]):
        """Log action attempt for audit trail"""
        logger.info(
            f"workshop_control_attempt",
            extra={
                "tool": self.__class__.__name__,
                "workshop_id": workshop_id,
                "user_id": user_id,
                "params": params,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
    
    def _log_action_success(self, workshop_id: int, user_id: int, result: ToolResult):
        """Log successful action"""
        logger.info(
            f"workshop_control_success",
            extra={
                "tool": self.__class__.__name__,
                "workshop_id": workshop_id,
                "user_id": user_id,
                "result_data": result.data,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
    
    def _log_action_failure(self, workshop_id: int, user_id: int, result: ToolResult):
        """Log failed action"""
        logger.warning(
            f"workshop_control_failure",
            extra={
                "tool": self.__class__.__name__,
                "workshop_id": workshop_id,
                "user_id": user_id,
                "error": result.error,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
    
    def _log_rbac_denial(self, workshop_id: int, user_id: int, reason: str):
        """Log RBAC denial for security monitoring"""
        logger.warning(
            f"workshop_control_rbac_denied",
            extra={
                "tool": self.__class__.__name__,
                "workshop_id": workshop_id,
                "user_id": user_id,
                "reason": reason,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
    
    # Task Information Helpers
    
    def _get_task_info(self, task: BrainstormTask) -> Dict[str, Any]:
        """Convert task to info dictionary"""
        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "task_type": task.task_type,
            "duration_seconds": task.duration,
            "status": task.status
        }
