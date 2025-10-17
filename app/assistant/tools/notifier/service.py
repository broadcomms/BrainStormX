from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict

from flask import current_app

from app.assistant.tools import BaseTool, ToolResult, ToolSchema
from app.assistant.tools.notifier.catalog import EVENT_SCHEMAS, EventType


class NotifierService(BaseTool):
    """Validated event dispatcher for workshop rooms."""

    def __init__(self, max_queue_size: int = 100) -> None:
        self._queue: Deque[Dict[str, Any]] = deque(maxlen=max_queue_size)

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="notify",
            namespace="notifier",
            description="Emit a notification event to workshop participants.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "event_type": {"type": "string", "enum": [event.value for event in EventType]},
                    "payload": {"type": "object"},
                },
                "required": ["workshop_id", "event_type", "payload"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "delivered": {"type": "boolean"},
                    "recipients": {"type": "integer"},
                },
            },
            requires_auth=False,
            requires_workshop=True,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        try:
            event_type = EventType(params["event_type"])
        except ValueError as exc:
            return ToolResult(success=False, error="Unsupported event type")

        schema = EVENT_SCHEMAS.get(event_type)
        if schema is None:
            return ToolResult(success=False, error=f"No schema registered for {event_type.value}")

        try:
            payload_model = schema(**params["payload"])
        except Exception as exc:
            return ToolResult(success=False, error=f"Invalid payload: {exc}")

        entry = {
            "workshop_id": params["workshop_id"],
            "event": event_type.value,
            "payload": payload_model.dict(),
        }

        if len(self._queue) == self._queue.maxlen:
            current_app.logger.warning("notifier_queue_full", extra={"dropped_event": entry["event"]})
        self._queue.append(entry)

        self._emit(entry)
        return ToolResult(success=True, data={"delivered": True, "recipients": 1})

    # ------------------------------------------------------------------
    def _emit(self, entry: Dict[str, Any]) -> None:
        from app.sockets_core.core import socketio

        try:
            workshop_id = entry["workshop_id"]
            event = entry["event"]
            payload = entry["payload"]

            # Broadcast across both legacy and current rooms/namespaces for compatibility
            rooms = [
                f"workshop_{workshop_id}",        # legacy room name
                f"workshop_room_{workshop_id}",   # current room name used by core
            ]
            namespaces = ["/workshop", "/"]

            for ns in namespaces:
                for room in rooms:
                    try:
                        socketio.emit(event, payload, room=room, namespace=ns)
                    except Exception as inner_exc:  # pragma: no cover - best-effort fanout
                        current_app.logger.debug(
                            "notifier_emit_attempt_failed", extra={"room": room, "ns": ns, "error": str(inner_exc)}
                        )
        except Exception as exc:  # pragma: no cover
            current_app.logger.warning("notifier_emit_failed", extra={"error": str(exc)})
