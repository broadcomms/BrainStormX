from __future__ import annotations

import logging
from typing import Any

from flask import current_app, has_app_context

from .metric import (
    idle_detections,
    phase_remaining_time,
    timer_starts,
    tool_invocations,
    tool_latency,
)

_logger = logging.getLogger("app.assistant.tools")


def _get_logger():
    if has_app_context():
        return current_app.logger
    return _logger


def record_tool_metric(event: str, **payload: Any) -> None:
    """Emit structured logs and forward counters to the metrics backend."""
    _get_logger().info(event, extra={"tool_metric": payload})

    tool_name = payload.get("tool_name") or payload.get("event_type") or event
    status = payload.get("status")
    if status is None:
        success = payload.get("success")
        if success is True:
            status = "success"
        elif success is False:
            status = "error"
        elif payload.get("error"):
            status = "error"
        else:
            status = "info"

    tool_invocations.labels(tool_name=str(tool_name), status=str(status)).inc()

    latency_ms = payload.get("latency_ms")
    if latency_ms is None:
        latency_value = None
    else:
        try:
            latency_value = float(latency_ms) / 1000.0
        except (TypeError, ValueError):
            latency_value = None
    if latency_value is not None:
        tool_latency.labels(tool_name=str(tool_name)).observe(latency_value)

    # specialized metrics
    if event == "phase_timing":
        workshop_id = payload.get("workshop_id")
        phase = payload.get("phase")
        remaining = payload.get("remaining_minutes")
        if workshop_id is not None and phase is not None and remaining is not None:
            try:
                phase_remaining_time.labels(str(workshop_id), str(phase)).observe(float(remaining))
            except Exception:  # pragma: no cover
                pass
    elif event == "idle_detection":
        workshop_id = payload.get("workshop_id")
        if workshop_id is not None:
            idle_detections.labels(str(workshop_id)).inc()
    elif event == "timer_started":
        workshop_id = payload.get("workshop_id")
        if workshop_id is not None:
            timer_starts.labels(str(workshop_id)).inc()
