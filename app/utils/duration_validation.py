"""Duration validation utilities for LLM-generated task durations.

Provides validation and consistency checking between numeric duration fields
and time references in narration text to prevent timer/narration mismatches.
"""

from __future__ import annotations

import json
import re
from typing import Dict, Optional, Tuple


_HYPHEN_CHARS = "-\u2010\u2011\u2012\u2013\u2014\u2015"


def _format_minutes(value: float) -> str:
    """Format minute values so tests can assert human-friendly strings."""

    if abs(value - round(value)) < 0.01:
        return f"{int(round(value))} min"
    return f"{value:.1f} min"


def extract_time_reference(text: str) -> Optional[int]:
    """Extract time reference in minutes from narration text."""

    if not text:
        return None

    hyphen_group = f"[{_HYPHEN_CHARS}]"
    pattern = rf"(\d+)\s*(?:{hyphen_group}\s*)?(?:minute|minutes|min|mins)\b"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))

    return None


def validate_narration_duration_consistency(
    narration: str,
    duration_seconds: Optional[int],
    tolerance_pct: float = 10.0,
) -> Tuple[bool, Optional[str]]:
    """Check if narration time reference matches actual task duration."""

    narration_minutes = extract_time_reference(narration or "")

    if narration_minutes is None:
        return True, "No time reference found in narration; treated as consistent with task duration."

    if duration_seconds is None:
        return False, (
            "Mismatch: narration mentions "
            f"{narration_minutes} min but task duration is missing."
        )

    actual_minutes = duration_seconds / 60.0
    if actual_minutes <= 0:
        return False, (
            "Mismatch: narration mentions "
            f"{narration_minutes} min but task duration is {actual_minutes:.1f} min."
        )

    baseline = narration_minutes if narration_minutes > 0 else actual_minutes
    if baseline == 0:
        baseline = 1
    diff_pct = abs(narration_minutes - actual_minutes) / baseline * 100

    if diff_pct > tolerance_pct:
        return False, (
            "Mismatch: narration mentions "
            f"{narration_minutes} min but task duration is {_format_minutes(actual_minutes)} "
            f"({diff_pct:.1f}% difference)."
        )

    return True, (
        "Narration and task duration are consistent "
        f"(mentions {narration_minutes} min, task duration {_format_minutes(actual_minutes)}; "
        f"difference {diff_pct:.1f}%)."
    )


def extract_agenda_durations(pre_workshop_data) -> Dict[str, int]:
    """Extract agenda durations as a mapping of titles to minutes."""

    try:
        data = json.loads(pre_workshop_data) if isinstance(pre_workshop_data, str) else pre_workshop_data
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}

    if not isinstance(data, dict):
        return {}

    agenda = data.get("agenda")
    if not isinstance(agenda, dict):
        return {}

    items = agenda.get("items", [])
    if not isinstance(items, list):
        return {}

    durations: Dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        duration_value = item.get("duration_minutes")
        if duration_value is None:
            duration_value = item.get("estimated_duration")

        if isinstance(duration_value, (int, float)) and duration_value > 0:
            title = item.get("activity_title") or item.get("title") or f"agenda_item_{index + 1}"
            durations[str(title)] = int(round(duration_value))

    return durations


def validate_duration_not_from_agenda(
    narration: str,
    task_duration_seconds: Optional[int],
    pre_workshop_data,
    tolerance_pct: float = 10.0,
) -> Tuple[bool, Optional[str]]:
    """Validate that LLM didn't use agenda duration for task narration."""

    agenda_durations = extract_agenda_durations(pre_workshop_data)
    narration_minutes = extract_time_reference(narration or "")
    actual_minutes = (task_duration_seconds / 60.0) if task_duration_seconds is not None else None

    def _matches_actual(agenda_minutes: int) -> bool:
        if actual_minutes is None or agenda_minutes <= 0:
            return False
        if actual_minutes == 0:
            return False
        diff_pct = abs(actual_minutes - agenda_minutes) / agenda_minutes * 100
        return diff_pct <= tolerance_pct

    def _matches_narration(agenda_minutes: int) -> bool:
        if narration_minutes is None:
            return False
        return narration_minutes == agenda_minutes

    matched_titles = [
        (title, minutes)
        for title, minutes in agenda_durations.items()
        if _matches_actual(minutes) or _matches_narration(minutes)
    ]

    if matched_titles:
        agenda_summary = ", ".join(f"{title} ({minutes} min)" for title, minutes in matched_titles)
        details = []
        if narration_minutes is not None:
            details.append(f"narration mentions {narration_minutes} min")
        if actual_minutes is not None:
            details.append(f"task duration {_format_minutes(actual_minutes)}")
        detail_clause = " and ".join(details) if details else "duration data"

        return False, (
            "Agenda duration detected: "
            f"{detail_clause} aligns with agenda item(s) {agenda_summary}. "
            "LLM should generate task-specific durations instead of reusing agenda estimates."
        )

    is_consistent, base_message = validate_narration_duration_consistency(
        narration, task_duration_seconds, tolerance_pct
    )

    if not agenda_durations:
        if is_consistent:
            return True, "No agenda durations found; narration duration is treated as unique."
        return False, base_message

    if not is_consistent:
        return False, base_message

    return True, "Narration duration appears unique compared to agenda durations."
