"""
Utility functions for handling workshop agenda data.

This module provides functions to strip confusing duration estimates from agenda
context before passing to LLMs, preventing duration confusion where LLMs read
high-level planning estimates instead of generating task-specific durations.
"""
import json
import logging
from typing import Any, Dict

from flask import current_app, has_app_context


logger = logging.getLogger(__name__)


def _log(level: str, message: str, *args) -> None:
    """Route logs through Flask when available, otherwise stdlib logging."""

    if has_app_context() and hasattr(current_app, "logger"):
        getattr(current_app.logger, level)(message, *args)
    else:
        getattr(logger, level)(message, *args)


def strip_agenda_durations(pre_workshop_data: str) -> str:
    """
    Remove duration_minutes and estimated_duration from agenda items in pre_workshop_data.
    
    This prevents LLMs from confusing high-level workshop phase planning durations
    (e.g., "15 minutes for entire Clustering & Voting phase") with individual
    task durations they should generate.
    
    Args:
        pre_workshop_data: JSON string containing workshop context with agenda
        
    Returns:
        JSON string with agenda durations removed, or original on error
        
    Example:
        Before: {"agenda": {"items": [{"title": "Clustering", "duration_minutes": 15}]}}
        After: {"agenda": {"items": [{"title": "Clustering"}]}}
    """
    try:
        context_dict = json.loads(pre_workshop_data) if isinstance(pre_workshop_data, str) else pre_workshop_data
        
        if isinstance(context_dict, dict) and 'agenda' in context_dict:
            agenda = context_dict['agenda']
            if isinstance(agenda, dict) and 'items' in agenda:
                items = agenda['items']
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            # Remove both duration fields that could confuse LLM
                            item.pop('duration_minutes', None)
                            item.pop('estimated_duration', None)
        
        return json.dumps(context_dict) if isinstance(context_dict, dict) else pre_workshop_data
        
    except Exception as exc:
        _log(
            "warning",
            "[AgendaUtils] Failed to strip agenda durations, using original: %s",
            exc,
        )
        return pre_workshop_data


def validate_task_duration(
    task_duration: Any,
    min_seconds: int = 30,
    max_seconds: int = 7200,
    strict_mode: bool = False
) -> int:
    """
    Validate and clamp task_duration value with explicit logging.
    
    Args:
        task_duration: Raw duration value from LLM (may be string, int, float, or None)
        min_seconds: Minimum allowed duration (default 30)
        max_seconds: Maximum allowed duration (default 7200 = 2 hours)
        strict_mode: If True, raise ValueError instead of using defaults
        
    Returns:
        Validated duration in seconds (clamped to min/max range)
        
    Raises:
        ValueError: If task_duration is invalid and strict_mode=True
    """
    if task_duration is None:
        if strict_mode:
            raise ValueError("task_duration is None")
        _log(
            "warning",
            "[AgendaUtils] task_duration is None, using default %d seconds",
            min_seconds,
        )
        return min_seconds

    try:
        duration_val = int(float(task_duration))
    except (ValueError, TypeError) as exc:
        if strict_mode:
            raise ValueError("Invalid task_duration") from exc
        _log(
            "warning",
            "[AgendaUtils] Invalid task_duration '%s', using default %d seconds: %s",
            task_duration,
            min_seconds,
            exc,
        )
        return min_seconds

    # Apply guardrails with explicit logging
    original_duration = duration_val
    duration_val = max(min_seconds, min(duration_val, max_seconds))

    if duration_val != original_duration:
        _log(
            "info",
            "[AgendaUtils] Duration clamped from %ds to %ds (range: %d-%d)",
            original_duration,
            duration_val,
            min_seconds,
            max_seconds,
        )

    return duration_val
