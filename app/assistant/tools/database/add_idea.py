from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError

from app.assistant.tools import BaseTool, ToolResult, ToolSchema
from app.assistant.tools.base import ToolExecutionError
from app.assistant.tools.notifier.catalog import EventType
from app.assistant.tools.telemetry import log_tool_event
from app.models import BrainstormIdea, BrainstormTask, Workshop, WorkshopParticipant, db

ALLOWED_IDEA_PHASES = {"brainstorming", "ideation", "warm_up"}


class AddIdeaTool(BaseTool):
    """Persist brainstorming ideas with guardrails and notifications."""

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="add_idea",
            namespace="database",
            description="Add a brainstorming idea to the current workshop task.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "text": {"type": "string", "minLength": 1, "maxLength": 500},
                    "participant_id": {"type": "integer", "minimum": 1},
                    "task_id": {"type": "integer", "minimum": 1},
                },
                "required": ["workshop_id", "text", "participant_id"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "idea_id": {"type": "integer"},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            },
            allowed_roles={"facilitator", "organizer", "participant"},
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop = self.ensure_workshop(params.get("workshop_id"))
        participant = self._ensure_participant(workshop.id, params["participant_id"])
        task = self._resolve_task(workshop, params.get("task_id"))

        try:
            with db.session.begin_nested():
                idea = BrainstormIdea(
                    task_id=task.id,
                    participant_id=participant.id,
                    content=params["text"].strip(),
                    source="assistant",
                    timestamp=datetime.utcnow(),
                )
                db.session.add(idea)
                db.session.flush()

                created_at = idea.timestamp.isoformat()
                idea_id = idea.id

            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return ToolResult(success=False, error="Database constraint violation")
        except Exception as exc:  # pragma: no cover
            db.session.rollback()
            return ToolResult(success=False, error=str(exc))

        result = ToolResult(
            success=True,
            data={"idea_id": idea_id, "created_at": created_at},
            rows_affected=1,
            metadata={
                "notifier": {
                    "event_type": EventType.IDEA_ADDED.value,
                    "payload": {
                        "id": idea_id,
                        "text": idea.content,
                        "contributor_id": participant.id,
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                }
            },
        )
        log_tool_event(
            "idea_created",
            {
                "workshop_id": workshop.id,
                "idea_id": idea_id,
                "participant_id": participant.id,
            },
        )
        return result

    # ------------------------------------------------------------------
    def _ensure_participant(self, workshop_id: int, participant_id: int) -> WorkshopParticipant:
        participant = db.session.get(WorkshopParticipant, participant_id)
        if not participant or participant.workshop_id != workshop_id:
            raise ToolExecutionError("Participant not part of workshop")
        return participant

    def _resolve_task(self, workshop: Workshop, task_id: Optional[int]) -> BrainstormTask:
        resolved_id = task_id or workshop.current_task_id
        if resolved_id is None:
            raise ToolExecutionError("No active brainstorming task for workshop")

        task = db.session.get(BrainstormTask, resolved_id)
        if not task or task.workshop_id != workshop.id:
            raise ToolExecutionError("Task not found for workshop")

        phase = (workshop.current_phase or "").lower()
        if phase and phase not in ALLOWED_IDEA_PHASES:
            raise ToolExecutionError(f"Cannot add ideas during {phase} phase")

        return task
