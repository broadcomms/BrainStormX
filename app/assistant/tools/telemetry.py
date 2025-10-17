from __future__ import annotations

import logging
from typing import Any, Dict

from app.assistant.tools.observability import record_tool_metric

_tool_logger = logging.getLogger("app.assistant.tools.events")


def log_tool_event(event: str, payload: Dict[str, Any]) -> None:
    """Reserve structured logging for tool-related events."""
    _tool_logger.info(event, extra={"tool_event": payload})
    record_tool_metric(event, **payload)
