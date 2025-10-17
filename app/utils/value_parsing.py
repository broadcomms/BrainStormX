"""Utility helpers for converting loosely-typed inputs into numeric values.

These helpers centralize the defensive parsing patterns used across the
service blueprints so mypy can reason about the resulting types while runtime
behavior remains permissive.
"""

from __future__ import annotations

from typing import Any


def safe_int(value: Any, default: int = 0) -> int:
    """Best-effort conversion to ``int`` with graceful fallback."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except (ValueError, TypeError):
            return default
    return default


def safe_float(value: Any, *, default: float | None = None) -> float | None:
    """Best-effort conversion to ``float`` with optional default on failure."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (ValueError, TypeError):
            return default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return float(stripped)
        except (ValueError, TypeError):
            return default
    return default


def bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Convert to ``int`` and clamp into ``[minimum, maximum]`` bounds."""
    result = safe_int(value, default=default)
    if result < minimum:
        return minimum
    if result > maximum:
        return maximum
    return result
