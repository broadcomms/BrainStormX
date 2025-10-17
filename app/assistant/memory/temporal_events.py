from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - optional dependency guard
    from bedrock_agentcore.memory import MemoryClient  # type: ignore
except Exception:  # pragma: no cover
    MemoryClient = None  # type: ignore

from app.config import Config


class TemporalMemoryService:
    """Store and query timestamped workshop events in AgentCore Memory."""

    def __init__(self) -> None:
        self.memory_id = Config.AGENTCORE_MEMORY_ID
        self.region = Config.AGENTCORE_MEMORY_REGION
        self._client = None
        if MemoryClient and self.memory_id:
            try:
                self._client = MemoryClient(region_name=self.region)
            except Exception:  # pragma: no cover - network/runtime failures during init
                self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self._client and self.memory_id)

    def store_temporal_event(
        self,
        workshop_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> None:
        if not self.enabled:
            return
        timestamp = timestamp or datetime.now(timezone.utc)
        actor_id = f"workshop-{workshop_id}"
        payload = {
            "event_type": event_type,
            "timestamp": timestamp.isoformat(),
            "workshop_id": workshop_id,
            "data": event_data,
        }
        try:
            self._client.create_event(  # type: ignore[operator]
                memory_id=self.memory_id,
                actor_id=actor_id,
                session_id=f"temporal-{workshop_id}",
                messages=[
                    {
                        "role": "system",
                        "content": [
                            {"type": "text", "text": f"Event: {event_type} at {timestamp.isoformat()}"}
                        ],
                    },
                    {
                        "role": "data",
                        "content": [
                            {"type": "text", "text": json.dumps(payload)}
                        ],
                    },
                ],
            )
        except Exception:  # pragma: no cover - do not break primary flow
            return

    def query_time_range(
        self,
        workshop_id: int,
        start_time: datetime,
        end_time: Optional[datetime] = None,
        event_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        end_time = end_time or datetime.now(timezone.utc)
        actor_id = f"workshop-{workshop_id}"
        namespace = f"support/user/{actor_id}/facts"
        query_parts = [f"workshop {workshop_id}", f"after {start_time.isoformat()}", f"before {end_time.isoformat()}"]
        if event_types:
            query_parts.append(f"events: {', '.join(event_types)}")
        query = " ".join(query_parts)
        try:
            memories = self._client.retrieve_memories(  # type: ignore[operator]
                memory_id=self.memory_id,
                namespace=namespace,
                query=query,
                top_k=50,
            )
        except Exception:  # pragma: no cover
            return []

        events: List[Dict[str, Any]] = []
        for entry in memories or []:
            content = entry.get("content") if isinstance(entry, dict) else None
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = None
            if not isinstance(content, dict):
                continue
            if self._event_in_range(content, start_time, end_time, event_types):
                events.append(content)
        return sorted(events, key=lambda item: item.get("timestamp", ""))

    @staticmethod
    def _event_in_range(
        event: Dict[str, Any],
        start_time: datetime,
        end_time: datetime,
        event_types: Optional[List[str]],
    ) -> bool:
        timestamp = event.get("timestamp")
        try:
            event_time = datetime.fromisoformat(timestamp)
        except Exception:  # pragma: no cover
            return False
        if not (start_time <= event_time <= end_time):
            return False
        if event_types and event.get("event_type") not in event_types:
            return False
        return True

    def get_recent_activity(self, workshop_id: int, minutes: int = 10) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=minutes)
        events = self.query_time_range(workshop_id, start, now)
        summary: Dict[str, Any] = {
            "time_window_minutes": minutes,
            "total_events": len(events),
            "events_by_type": {},
            "last_event": events[-1] if events else None,
        }
        for event in events:
            event_type = event.get("event_type", "unknown")
            summary["events_by_type"][event_type] = summary["events_by_type"].get(event_type, 0) + 1
        return summary
