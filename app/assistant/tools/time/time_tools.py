from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Any

from app.assistant.tools import BaseTool, ToolResult, ToolSchema
from app.assistant.tools.observability import record_tool_metric
from app.assistant.time_context import TimeContextProvider
from app.assistant.memory.temporal_events import TemporalMemoryService
from app.config import Config
from app.extensions import socketio


class GetCurrentTimeTool(BaseTool):
    """Return the current time in UTC and local timezone."""

    def __init__(self) -> None:
        self.time_provider = TimeContextProvider()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_current_time",
            namespace="time",
            description="Get current time in UTC and the configured local timezone.",
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone identifier (default from config)",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "utc": {"type": "string"},
                    "local": {"type": "string"},
                    "unix": {"type": "integer"},
                    "timezone": {"type": "string"},
                },
            },
            requires_auth=False,
            requires_workshop=False,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        tz = params.get("timezone") or Config.DEFAULT_TIMEZONE
        provider = TimeContextProvider(default_timezone=tz)
        context = provider.get_time_context()
        return ToolResult(
            success=True,
            data={
                "utc": context.get("current_time_utc"),
                "local": context.get("current_time_local"),
                "unix": context.get("timestamp_unix"),
                "timezone": context.get("local_timezone"),
            },
        )


class GetPhaseTimingTool(BaseTool):
    """Return timing information for the active workshop phase."""

    def __init__(self) -> None:
        self.time_provider = TimeContextProvider()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_phase_timing",
            namespace="time",
            description="Inspect remaining time in the current workshop phase.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                },
                "required": ["workshop_id"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "phase": {"type": "string"},
                    "remaining_minutes": {"type": "integer"},
                    "elapsed_minutes": {"type": "integer"},
                    "is_overrun": {"type": "boolean"},
                },
            },
            requires_auth=False,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop_id = params["workshop_id"]
        context = self.time_provider.get_time_context(workshop_id)
        schedule = context.get("workshop_schedule", {}) if isinstance(context, dict) else {}
        remaining = schedule.get("remaining_minutes_in_phase")
        overrun = schedule.get("phase_overrun_minutes", 0)
        data = {
            "phase": schedule.get("current_phase", "unknown"),
            "remaining_minutes": remaining if isinstance(remaining, int) else 0,
            "elapsed_minutes": schedule.get("elapsed_minutes", 0),
            "is_overrun": bool(overrun),
        }
        record_tool_metric(
            "phase_timing",
            workshop_id=workshop_id,
            phase=data["phase"],
            remaining_minutes=data["remaining_minutes"],
        )
        result = ToolResult(
            success=True,
            data=data,
        )
        return result


class QueryRecentActivityTool(BaseTool):
    """Summarise recent temporal events for a workshop."""

    def __init__(self) -> None:
        self.temporal_memory = TemporalMemoryService()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="query_recent_activity",
            namespace="time",
            description="Summarise events captured in AgentCore Memory over a time window.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "minutes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                        "default": 10,
                    },
                },
                "required": ["workshop_id"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "time_window_minutes": {"type": "integer"},
                    "total_events": {"type": "integer"},
                    "events_by_type": {"type": "object"},
                    "last_event": {"type": "object"},
                },
            },
            requires_auth=False,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        if not self.temporal_memory.enabled:
            return ToolResult(success=False, error="Temporal memory is not configured")
        summary = self.temporal_memory.get_recent_activity(
            params["workshop_id"],
            params.get("minutes", 10),
        )
        if summary.get("total_events", 0) == 0:
            record_tool_metric(
                "idle_detection",
                workshop_id=params["workshop_id"],
            )
        return ToolResult(success=True, data=summary)


class StartTimerTool(BaseTool):
    """Start a countdown timer for the workshop."""

    def __init__(self) -> None:
        self.temporal_memory = TemporalMemoryService()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="start_timer",
            namespace="time",
            description="Start a countdown timer and broadcast it to participants.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
                    "message": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                "required": ["workshop_id", "duration_minutes"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "timer_id": {"type": "string"},
                    "end_time": {"type": "string"},
                },
            },
            requires_auth=False,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop_id = params["workshop_id"]
        duration = params["duration_minutes"]
        message = params.get("message", f"{duration} minute timer")
        timer_id = f"timer-{workshop_id}-{int(datetime.now().timestamp())}"
        end_time = datetime.now(timezone.utc) + timedelta(minutes=duration)

        socketio.emit(  # type: ignore[call-arg]
            "timer_started",
            {
                "timer_id": timer_id,
                "duration_minutes": duration,
                "end_time": end_time.isoformat(),
                "message": message,
            },
            to=f"workshop_{workshop_id}",
            namespace="/workshop",
        )

        if self.temporal_memory.enabled:
            self.temporal_memory.store_temporal_event(
                workshop_id,
                "timer_started",
                {
                    "timer_id": timer_id,
                    "duration_minutes": duration,
                    "message": message,
                },
            )

        record_tool_metric(
            "timer_started",
            workshop_id=workshop_id,
            timer_id=timer_id,
            duration_minutes=duration,
        )

        return ToolResult(
            success=True,
            data={"timer_id": timer_id, "end_time": end_time.isoformat()},
            rows_affected=1,
        )


class ScheduleReminderTool(BaseTool):
    """Schedule a reminder event for the workshop participants."""

    def __init__(self) -> None:
        self.temporal_memory = TemporalMemoryService()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="schedule_reminder",
            namespace="time",
            description="Schedule a reminder to be broadcast after a delay.",
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    "minutes_from_now": {"type": "integer", "minimum": 1, "maximum": 60},
                    "message": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["workshop_id", "minutes_from_now", "message"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "string"},
                    "scheduled_time": {"type": "string"},
                },
            },
            requires_auth=False,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop_id = params["workshop_id"]
        minutes_from_now = params["minutes_from_now"]
        message = params["message"]

        reminder_id = f"reminder-{workshop_id}-{int(datetime.now().timestamp())}"
        scheduled_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)

        socketio.emit(  # type: ignore[call-arg]
            "reminder_scheduled",
            {
                "reminder_id": reminder_id,
                "scheduled_time": scheduled_time.isoformat(),
                "message": message,
                "minutes_from_now": minutes_from_now,
            },
            to=f"workshop_{workshop_id}",
            namespace="/workshop",
        )

        if self.temporal_memory.enabled:
            self.temporal_memory.store_temporal_event(
                workshop_id,
                "reminder_scheduled",
                {
                    "reminder_id": reminder_id,
                    "scheduled_time": scheduled_time.isoformat(),
                    "message": message,
                },
            )

        return ToolResult(
            success=True,
            data={"reminder_id": reminder_id, "scheduled_time": scheduled_time.isoformat()},
            rows_affected=1,
        )
