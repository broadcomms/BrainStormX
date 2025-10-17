from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from app.assistant.memory.temporal_events import TemporalMemoryService
from app.sockets_core.core import socketio

temporal_memory = TemporalMemoryService()


def _store_event(workshop_id: int, event_type: str, data: Dict[str, Any]) -> None:
    if not temporal_memory.enabled:
        return
    temporal_memory.store_temporal_event(
        workshop_id,
        event_type,
        data,
        timestamp=datetime.now(timezone.utc),
    )


@socketio.on("heartbeat", namespace="/workshop")
def handle_heartbeat(data: Dict[str, Any]) -> None:
    workshop_id = data.get("workshop_id")
    if not workshop_id:
        return
    _store_event(
        int(workshop_id),
        "heartbeat",
        {
            "client_timestamp": data.get("timestamp"),
            "server_timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@socketio.on("idea_added", namespace="/workshop")
def handle_idea_added_with_time(data: Dict[str, Any]) -> None:
    workshop_id = data.get("workshop_id")
    if not workshop_id:
        return
    _store_event(
        int(workshop_id),
        "idea_added",
        {
            "idea_id": data.get("id"),
            "text": (data.get("text") or "")[:100],
            "contributor_id": data.get("contributor_id"),
        },
    )


@socketio.on("vote_cast", namespace="/workshop")
def handle_vote_cast_with_time(data: Dict[str, Any]) -> None:
    workshop_id = data.get("workshop_id")
    if not workshop_id:
        return
    _store_event(
        int(workshop_id),
        "vote_cast",
        {
            "cluster_id": data.get("cluster_id"),
            "user_id": data.get("user_id"),
            "vote_count": data.get("vote_count"),
        },
    )
