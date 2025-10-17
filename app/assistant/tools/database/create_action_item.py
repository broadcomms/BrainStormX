from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.assistant.tools import BaseTool, ToolResult, ToolSchema
from app.assistant.tools.base import ToolExecutionError
from app.assistant.tools.notifier.catalog import EventType
from app.models import ActionItem, WorkshopParticipant, db


class CreateActionItemTool(BaseTool):
    """Create actionable follow-ups tied to a workshop."""

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="create_action_item",
            namespace="database",
            description="Create an action item for the workshop backlog.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {"type": "string", "maxLength": 1000},
                    "owner_participant_id": {"type": "integer", "minimum": 1},
                    "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                    "due_date": {"type": "string", "format": "date"},
                },
                "required": ["workshop_id", "title"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "action_id": {"type": "integer"},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            },
            allowed_roles={"facilitator", "organizer"},
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop = self.ensure_workshop(params.get("workshop_id"))

        owner: Optional[WorkshopParticipant] = None
        if "owner_participant_id" in params:
            owner = self._ensure_participant(workshop.id, params["owner_participant_id"])

        due_date = self._parse_due_date(params.get("due_date"))

        try:
            with db.session.begin_nested():
                action = ActionItem(
                    workshop_id=workshop.id,
                    title=params["title"].strip(),
                    description=params.get("description", "").strip(),
                    owner_participant_id=owner.id if owner else None,
                    priority=params.get("priority", "medium"),
                    status="todo",
                    created_at=datetime.utcnow(),
                    due_date=due_date,
                )
                db.session.add(action)
                db.session.flush()
                created_at = action.created_at.isoformat()
                action_id = action.id
            db.session.commit()
        except Exception as exc:  # pragma: no cover - defensive path
            db.session.rollback()
            return ToolResult(success=False, error=str(exc))

        result = ToolResult(
            success=True,
            data={"action_id": action_id, "created_at": created_at},
            rows_affected=1,
            metadata={
                "notifier": {
                    "event_type": EventType.ACTION_CREATED.value,
                    "payload": {
                        "id": action_id,
                        "title": action.title,
                        "owner_id": owner.id if owner else None,
                        "priority": action.priority,
                    },
                }
            },
        )
        log_tool_event(
            "action_item_created",
            {
                "workshop_id": workshop.id,
                "action_id": action_id,
                "priority": action.priority,
                "owner_participant_id": owner.id if owner else None,
            },
        )
        return result

    # ------------------------------------------------------------------
    def _ensure_participant(self, workshop_id: int, participant_id: int) -> WorkshopParticipant:
        participant = db.session.get(WorkshopParticipant, participant_id)
        if not participant or participant.workshop_id != workshop_id:
            raise ToolExecutionError("Owner must be a workshop participant")
        return participant

    def _parse_due_date(self, raw: Optional[str]):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError as exc:
            raise ToolExecutionError("Invalid due_date format; expected YYYY-MM-DD") from exc
