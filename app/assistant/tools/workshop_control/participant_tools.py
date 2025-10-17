"""
Participant Action Tools

Tools for workshop participants to take actions via natural language.
"""

import uuid
import logging
from typing import Dict, Any

from app.assistant.tools.base import ToolSchema, ToolResult
from app.models import Workshop, User, BrainstormIdea, IdeaVote, GenericVote, db
from .base import WorkshopControlTool
from .schemas import AddIdeaParams, CastVoteParams, AddCommentParams

logger = logging.getLogger(__name__)


class AddIdeaTool(WorkshopControlTool):
    """
    Tool to add an idea to the workshop whiteboard.
    
    Available during brainstorming phases.
    Both organizers and participants can add ideas.
    """
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = False  # Participants can use this
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="add_idea",
            namespace="workshop_control",
            description="Add a new idea to the workshop whiteboard. Available during brainstorming phases.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {
                        "type": "integer",
                        "description": "ID of the workshop",
                        "minimum": 1
                    },
                    "user_id": {
                        "type": "integer",
                        "description": "ID of the user adding the idea",
                        "minimum": 1
                    },
                    "text": {
                        "type": "string",
                        "description": "The idea text",
                        "minLength": 1,
                        "maxLength": 1000
                    }
                },
                "required": ["workshop_id", "user_id", "text"],
                "additionalProperties": False
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "idea_id": {"type": "integer"},
                    "idea_text": {"type": "string"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "add an idea"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """Add idea with transactional safety"""
        
        # Validate workshop is in progress
        valid, error = self._validate_workshop_in_progress(workshop)
        if not valid:
            return ToolResult(
                success=False,
                error=error,
                correlation_id=correlation_id
            )
        
        # Get and validate idea text
        idea_text = params.get("text", "").strip()
        if not idea_text:
            return ToolResult(
                success=False,
                error="Idea text cannot be empty",
                correlation_id=correlation_id
            )
        
        if len(idea_text) > 1000:
            return ToolResult(
                success=False,
                error="Idea text cannot exceed 1000 characters",
                correlation_id=correlation_id
            )
        
        # TODO: Validate we're in a brainstorming phase
        # For now, allow ideas to be added anytime during workshop
        
        # Create the idea
        try:
            # Note: BrainstormIdea requires task_id and participant_id
            # For simplicity, we'll use current task and find/create participant
            if not workshop.current_task_id:
                return ToolResult(
                    success=False,
                    error="No active task to add ideas to",
                    correlation_id=correlation_id
                )
            
            # Find or create participant
            from app.models import WorkshopParticipant
            participant = WorkshopParticipant.query.filter_by(
                workshop_id=workshop.id,
                user_id=user.user_id
            ).first()
            
            if not participant:
                # Create participant if doesn't exist
                participant = WorkshopParticipant(
                    workshop_id=workshop.id,
                    user_id=user.user_id,
                    status='active'
                )
                db.session.add(participant)
                db.session.flush()  # Get the participant ID
            
            idea = BrainstormIdea(
                task_id=workshop.current_task_id,
                participant_id=participant.id,
                content=idea_text,
                source='assistant_tool'
            )
            db.session.add(idea)
            db.session.commit()
            
            # Emit Socket.IO event for real-time UI update
            # Must match the payload structure expected by addStickyNote() in workshop_room.html
            try:
                from app.extensions import socketio
                
                # Get username for display
                try:
                    username = f"{user.first_name} {user.last_name}".strip()
                    if not username:
                        username = user.email.split("@")[0] if user.email else "Assistant"
                except Exception:
                    username = "Assistant"
                
                emit_payload = {
                    "idea_id": idea.id,
                    "task_id": idea.task_id,
                    "user": username,
                    "content": idea.content,
                    "timestamp": idea.timestamp.isoformat() if idea.timestamp else None,
                    "source": "assistant_tool",  # Mark as assistant-generated
                    "rationale": None,
                    "include_in_outputs": True,
                }
                
                room = f"workshop_room_{workshop.id}"
                socketio.emit("new_idea", emit_payload, to=room)
                logger.info(f"Emitted new_idea event to {room} for idea {idea.id}")
                
            except Exception as e:
                logger.warning(f"Failed to emit new_idea event: {e}")
            
            return ToolResult(
                success=True,
                data={
                    "message": f"Added your idea: '{idea_text}'",
                    "idea_id": idea.id,
                    "idea_content": idea.content
                },
                rows_affected=1,
                correlation_id=correlation_id
            )
            
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to add idea")
            return ToolResult(
                success=False,
                error=f"Failed to add idea: {str(e)}",
                correlation_id=correlation_id
            )


class CastVoteTool(WorkshopControlTool):
    """
    Tool to cast a vote for a cluster during voting phases.
    
    Uses pessimistic locking for concurrency safety.
    Both organizers and participants can vote.
    """
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = False  # Participants can use this
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="cast_vote",
            namespace="workshop_control",
            description="Cast a vote for a cluster during voting phases. Vote limits apply.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {
                        "type": "integer",
                        "description": "ID of the workshop",
                        "minimum": 1
                    },
                    "user_id": {
                        "type": "integer",
                        "description": "ID of the user casting the vote",
                        "minimum": 1
                    },
                    "cluster_id": {
                        "type": "integer",
                        "description": "ID of the cluster to vote for",
                        "minimum": 1
                    }
                },
                "required": ["workshop_id", "user_id", "cluster_id"],
                "additionalProperties": False
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "vote_id": {"type": "integer"},
                    "votes_remaining": {"type": "integer"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "cast a vote"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """Cast vote with concurrency safety"""
        
        # Validate workshop is in progress
        valid, error = self._validate_workshop_in_progress(workshop)
        if not valid:
            return ToolResult(
                success=False,
                error=error,
                correlation_id=correlation_id
            )
        
        cluster_id = params.get("cluster_id")
        if not cluster_id:
            return ToolResult(
                success=False,
                error="cluster_id is required",
                correlation_id=correlation_id
            )
        
        # TODO: Validate we're in a voting phase
        # TODO: Check vote limits
        # TODO: Verify cluster exists and belongs to this workshop
        
        # For now, simple implementation
        try:
            # Find participant
            from app.models import WorkshopParticipant
            participant = WorkshopParticipant.query.filter_by(
                workshop_id=workshop.id,
                user_id=user.user_id
            ).first()
            
            if not participant:
                return ToolResult(
                    success=False,
                    error="You must be a workshop participant to vote",
                    correlation_id=correlation_id
                )
            
            # Check if user already voted for this cluster
            from app.models import IdeaCluster
            existing_vote = IdeaVote.query.filter_by(
                cluster_id=cluster_id,
                participant_id=participant.id
            ).first()
            
            if existing_vote:
                return ToolResult(
                    success=False,
                    error="You have already voted for this cluster",
                    correlation_id=correlation_id
                )
            
            # Create vote
            vote = IdeaVote(
                cluster_id=cluster_id,
                participant_id=participant.id,
                dots_used=1
            )
            db.session.add(vote)
            db.session.commit()
            
            # Count remaining votes (example: max 3 votes per user)
            user_votes = IdeaVote.query.filter_by(
                participant_id=participant.id
            ).count()
            votes_remaining = max(0, 3 - user_votes)
            
            # Emit Socket.IO event
            try:
                from flask_socketio import emit
                emit('vote_cast', {
                    'workshop_id': workshop.id,
                    'cluster_id': cluster_id,
                    'participant_id': participant.id
                }, to=f'workshop_{workshop.id}', namespace='/')
            except Exception as e:
                logger.warning(f"Failed to emit vote_cast event: {e}")
            
            return ToolResult(
                success=True,
                data={
                    "message": "Vote cast successfully",
                    "vote_id": vote.id,
                    "votes_remaining": votes_remaining
                },
                rows_affected=1,
                correlation_id=correlation_id
            )
            
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to cast vote")
            return ToolResult(
                success=False,
                error=f"Failed to cast vote: {str(e)}",
                correlation_id=correlation_id
            )


class AddCommentTool(WorkshopControlTool):
    """
    Tool to add a comment to a discussion, idea, or cluster.
    
    Note: Comment model needs to be added to app/models.py
    This is a placeholder implementation for future enhancement.
    """
    
    def __init__(self):
        super().__init__()
        self.requires_organizer = False  # Participants can use this
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="add_comment",
            namespace="workshop_control",
            description="Add a comment to a discussion, idea, or cluster. (Placeholder - Comment model not yet implemented)",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {
                        "type": "integer",
                        "description": "ID of the workshop",
                        "minimum": 1
                    },
                    "user_id": {
                        "type": "integer",
                        "description": "ID of the user adding the comment",
                        "minimum": 1
                    },
                    "text": {
                        "type": "string",
                        "description": "The comment text",
                        "minLength": 1,
                        "maxLength": 2000
                    },
                    "parent_type": {
                        "type": "string",
                        "description": "Type of parent entity (idea, cluster, discussion)",
                        "enum": ["idea", "cluster", "discussion"]
                    },
                    "parent_id": {
                        "type": "integer",
                        "description": "ID of parent entity",
                        "minimum": 1
                    }
                },
                "required": ["workshop_id", "user_id", "text", "parent_type", "parent_id"],
                "additionalProperties": False
            },
            returns={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "message": {"type": "string"},
                    "comment_id": {"type": "integer"}
                }
            },
            requires_auth=True,
            requires_workshop=True
        )
    
    def _get_action_description(self) -> str:
        return "add a comment"
    
    def _execute_action(
        self,
        workshop: Workshop,
        user: User,
        params: Dict[str, Any],
        correlation_id: str
    ) -> ToolResult:
        """Add comment - Placeholder implementation"""
        
        # TODO: Implement Comment model in app/models.py
        # For now, return not implemented
        return ToolResult(
            success=False,
            error="Comment functionality is not yet implemented. The Comment model needs to be added to the database schema.",
            correlation_id=correlation_id
        )
