# app/assistant/tools/metrics.py
from __future__ import annotations

from typing import Any

from flask import Blueprint, Response

try:  # pragma: no cover - optional dependency handling
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - dependency not installed
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    generate_latest = lambda: b""  # type: ignore

    class _NoOpMetric:
        def labels(self, *args: Any, **kwargs: Any) -> "_NoOpMetric":
            return self

        def inc(self, value: float = 1.0) -> None:
            return None

        def observe(self, value: float) -> None:
            return None

    PROMETHEUS_ENABLED = False
    tool_invocations = _NoOpMetric()
    tool_latency = _NoOpMetric()
    phase_remaining_time = _NoOpMetric()
    idle_detections = _NoOpMetric()
    timer_starts = _NoOpMetric()
else:
    PROMETHEUS_ENABLED = True
    tool_invocations = Counter(
        "tool_invocations_total",
        "Total tool invocations",
        ["tool_name", "status"],
    )
    tool_latency = Histogram(
        "tool_latency_seconds",
        "Tool execution latency",
        ["tool_name"],
    )
    phase_remaining_time = Histogram(
        "workshop_phase_remaining_minutes",
        "Remaining time in the current workshop phase",
        ["workshop_id", "phase"],
    )
    idle_detections = Counter(
        "workshop_idle_detections_total",
        "Idle period detections",
        ["workshop_id"],
    )
    timer_starts = Counter(
        "workshop_timers_started_total",
        "Number of timers started",
        ["workshop_id"],
    )


# Blueprint for metrics endpoint
metrics_bp = Blueprint("metrics", __name__)


@metrics_bp.route("/metrics")
def metrics() -> Response:
    payload = generate_latest()
    status = 200 if PROMETHEUS_ENABLED else 503
    return Response(payload, status=status, mimetype=CONTENT_TYPE_LATEST)


__all__ = [
    "PROMETHEUS_ENABLED",
    "metrics_bp",
    "tool_invocations",
    "tool_latency",
    "phase_remaining_time",
    "idle_detections",
    "timer_starts",
]
