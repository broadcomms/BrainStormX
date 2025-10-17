from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.assistant.tools.base import BaseTool
from app.assistant.tools.types import ToolResult, ToolSchema
from app.models import WorkshopAgenda


class GetAgendaTool(BaseTool):
    """Return the structured agenda items that have been saved for a workshop."""

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_agenda",
            namespace="workshop",
            description=(
                "Retrieve the current workshop agenda, including titles, optional descriptions, "
                "and estimated durations when available. Falls back to the legacy text agenda if "
                "no normalized rows have been saved."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    # Gateway injects user_id for auth'd calls; accept it to satisfy validation
                    "user_id": {"type": "integer", "minimum": 1},
                    "include_descriptions": {"type": "boolean", "default": True},
                },
                "required": ["workshop_id"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "items": {"type": "array"},
                    "total_estimated_minutes": {"type": "integer"},
                    "source": {"type": "string"},
                    "count": {"type": "integer"},
                },
            },
            requires_auth=True,
            requires_workshop=True,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop_id = params.get("workshop_id")
        workshop = self.ensure_workshop(workshop_id)

        include_descriptions = bool(params.get("include_descriptions", True))

        rows = (
            WorkshopAgenda.query
            .filter_by(workshop_id=workshop.id)
            .order_by(WorkshopAgenda.position.asc())
            .all()
        )

        items: List[Dict[str, Any]] = []
        total_minutes = 0
        source = "agenda_rows"

        if rows:
            for row in rows:
                minutes = self._coerce_int(row.estimated_duration)
                if minutes is None:
                    minutes = self._coerce_int(getattr(row, "duration_minutes", None))
                if minutes is not None and minutes > 0:
                    total_minutes += minutes

                description: Optional[str]
                if include_descriptions:
                    description = (row.activity_description or "").strip() or None
                else:
                    description = None

                items.append(
                    {
                        "id": row.id,
                        "position": row.position,
                        "title": (row.activity_title or "").strip(),
                        "description": description,
                        "estimated_minutes": minutes,
                        "time_slot": row.time_slot,
                    }
                )
        else:
            source = "workshop.agenda"
            raw_agenda = getattr(workshop, "agenda", None)
            parsed_items: List[Dict[str, Any]] = []
            if isinstance(raw_agenda, str) and raw_agenda.strip():
                parsed_items = self._parse_legacy_agenda(raw_agenda)

            if parsed_items:
                for idx, entry in enumerate(parsed_items, start=1):
                    minutes = self._coerce_int(entry.get("estimated_duration"))
                    if minutes is not None and minutes > 0:
                        total_minutes += minutes
                    description = entry.get("activity_description") if include_descriptions else None
                    items.append(
                        {
                            "id": entry.get("id"),
                            "position": idx,
                            "title": entry.get("activity_title") or entry.get("title") or "",
                            "description": description,
                            "estimated_minutes": minutes,
                            "time_slot": entry.get("time_slot"),
                        }
                    )
            else:
                # Fall back to treating each non-empty line as a title-only entry
                lines = [line.strip() for line in (raw_agenda or "").splitlines() if line.strip()]
                for idx, title in enumerate(lines, start=1):
                    items.append(
                        {
                            "id": None,
                            "position": idx,
                            "title": title,
                            "description": None if not include_descriptions else None,
                            "estimated_minutes": None,
                            "time_slot": None,
                        }
                    )

        result_payload = {
            "items": items,
            "total_estimated_minutes": total_minutes,
            "source": source,
            "count": len(items),
        }
        return ToolResult(success=True, data=result_payload)

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip():
                return int(float(value))
        except (TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _parse_legacy_agenda(raw: str) -> List[Dict[str, Any]]:
        """Parse the legacy workshop.agenda field which may store JSON or plain text."""
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []

        if isinstance(parsed, dict) and isinstance(parsed.get("agenda"), list):
            normalized: List[Dict[str, Any]] = []
            for entry in parsed.get("agenda", []):
                if not isinstance(entry, dict):
                    continue
                normalized.append({
                    "id": entry.get("id"),
                    "activity_title": entry.get("activity_title") or entry.get("activity") or entry.get("title"),
                    "activity_description": entry.get("activity_description") or entry.get("description"),
                    "estimated_duration": entry.get("estimated_duration"),
                    "time_slot": entry.get("time_slot"),
                })
            return normalized
        return []
