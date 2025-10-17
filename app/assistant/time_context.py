from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - fallback for very old environments
    ZoneInfo = None  # type: ignore

from app.config import Config
from app.extensions import db
from app.models import Workshop
from app.tasks.registry import TASK_REGISTRY


class TimeContextProvider:
    """Derive temporal context for assistant decision making."""

    def __init__(self, default_timezone: Optional[str] = None) -> None:
        self.default_timezone = default_timezone or Config.DEFAULT_TIMEZONE

    def get_time_context(self, workshop_id: Optional[int] = None) -> Dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        local_time = self._to_local(now_utc)

        context: Dict[str, Any] = {
            "current_time_utc": now_utc.isoformat(),
            "current_time_local": local_time.isoformat(),
            "local_timezone": self.default_timezone,
            "timestamp_unix": int(now_utc.timestamp()),
            "day_of_week": local_time.strftime("%A"),
            "time_of_day": self._categorize_time(local_time.hour),
        }

        if workshop_id is not None:
            schedule = self._build_workshop_schedule(workshop_id, now_utc)
            if schedule:
                context["workshop_schedule"] = schedule

        return context

    def _to_local(self, dt: datetime) -> datetime:
        if ZoneInfo is None:
            return dt
        try:
            tz = ZoneInfo(self.default_timezone)
            return dt.astimezone(tz)
        except Exception:  # pragma: no cover - defensive catch for invalid tz
            return dt

    @staticmethod
    def _categorize_time(hour: int) -> str:
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 21:
            return "evening"
        return "night"

    def _build_workshop_schedule(self, workshop_id: int, now_utc: datetime) -> Optional[Dict[str, Any]]:
        workshop = db.session.get(Workshop, workshop_id)
        if not workshop:
            return None

        workshop_started_at = self._ensure_aware(getattr(workshop, "created_at", None))
        workshop_elapsed = (
            now_utc - workshop_started_at
            if workshop_started_at and now_utc >= workshop_started_at
            else timedelta()
        )
        workshop_elapsed_minutes = max(0, int(workshop_elapsed.total_seconds() // 60))

        current_phase_id = getattr(workshop, "current_phase", None) or getattr(workshop, "current_task_id", None)
        
        # Get actual task details for better context
        current_task = None
        phase_title = None
        phase_description = None
        if current_phase_id and str(current_phase_id).isdigit():
            from app.models import BrainstormTask
            current_task = db.session.get(BrainstormTask, current_phase_id)
            if current_task:
                phase_title = current_task.title
                phase_description = current_task.description
        
        # Handle pre-workshop-start state when no tasks are active yet
        if not current_phase_id and workshop.status == "inprogress":
            current_phase_id = "waiting_to_start"
            phase_title = "Waiting to Start"
            phase_description = "Waiting for organizer to begin workshop"
        
        phase_started_at = self._ensure_aware(getattr(workshop, "phase_started_at", None))
        if not phase_started_at and current_task and getattr(current_task, "started_at", None):
            phase_started_at = self._ensure_aware(current_task.started_at)
        task_meta = TASK_REGISTRY.get(str(current_phase_id)) or TASK_REGISTRY.get(str(current_phase_id).lower()) if current_phase_id else None
        if not task_meta and current_phase_id:
            task_meta = TASK_REGISTRY.get(current_phase_id.split(":" )[0]) if isinstance(current_phase_id, str) else None

        phase_elapsed = (
            now_utc - phase_started_at
            if phase_started_at and now_utc >= phase_started_at
            else timedelta()
        )
        phase_elapsed_minutes = max(0, int(phase_elapsed.total_seconds() // 60))

        phase_duration_minutes: Optional[int] = None
        duration_seconds: Optional[int] = None
        if current_task and isinstance(getattr(current_task, "duration", None), int):
            # BrainstormTask.duration is stored in seconds
            duration_seconds = max(int(current_task.duration), 0)
            if duration_seconds:
                phase_duration_minutes = max(1, math.ceil(duration_seconds / 60))
        elif task_meta:
            default_duration = task_meta.get("default_duration")
            if isinstance(default_duration, int) and default_duration > 0:
                if default_duration >= 180:
                    phase_duration_minutes = max(1, default_duration // 60)
                    duration_seconds = default_duration if default_duration >= 180 else default_duration * 60
                else:
                    phase_duration_minutes = default_duration
                    duration_seconds = default_duration * 60

        schedule: Dict[str, Any] = {
            "workshop_id": workshop_id,
            "status": getattr(workshop, "status", None),
            "started_at": workshop_started_at.isoformat() if workshop_started_at else None,
            "elapsed_minutes": phase_elapsed_minutes,
            "elapsed_formatted": self._format_duration(phase_elapsed),
            "current_phase": current_phase_id or "unknown",
            "phase_title": phase_title or "Unknown Phase",
            "phase_description": phase_description,
            "phase_started_at": phase_started_at.isoformat() if phase_started_at else None,
            "workshop_elapsed_minutes": workshop_elapsed_minutes,
            "workshop_elapsed_formatted": self._format_duration(workshop_elapsed) if workshop_started_at else None,
        }

        # Special handling for waiting_to_start state
        if current_phase_id == "waiting_to_start":
            schedule["remaining_minutes_in_phase"] = None
        elif phase_duration_minutes:
            schedule["phase_duration_minutes"] = phase_duration_minutes
            if duration_seconds and phase_started_at:
                deadline = phase_started_at + timedelta(seconds=duration_seconds)
                schedule["phase_deadline"] = deadline.isoformat()

            remaining_seconds = None
            if duration_seconds is not None:
                elapsed_seconds = int(phase_elapsed.total_seconds())
                remaining_seconds = duration_seconds - elapsed_seconds

            if remaining_seconds is None:
                remaining_seconds = (phase_duration_minutes * 60) - int(phase_elapsed.total_seconds())

            if remaining_seconds >= 0:
                schedule["remaining_minutes_in_phase"] = max(0, remaining_seconds // 60)
            else:
                schedule["remaining_minutes_in_phase"] = 0
                schedule["phase_overrun_minutes"] = max(1, math.ceil(abs(remaining_seconds) / 60))
        else:
            schedule["remaining_minutes_in_phase"] = None

        return schedule

    @staticmethod
    def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
        if not dt:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _format_duration(duration: timedelta) -> str:
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
