from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable

from flask import current_app, has_app_context

from .base import BaseTool
from .observability import record_tool_metric
from .telemetry import log_tool_event
from .types import ToolInvocation, ToolResult, ToolSchema


logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for tool discovery and execution."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}
        self._schemas: Dict[str, ToolSchema] = {}

    # ------------------------------------------------------------------
    # Registration & discovery
    # ------------------------------------------------------------------
    def register(self, tool: BaseTool) -> None:
        schema = tool.get_schema()
        key = schema.full_name
        if key in self._tools:
            raise ValueError(f"Tool already registered: {key}")
        self._tools[key] = tool
        self._schemas[key] = schema
        self._log("info", "tool_registered", {"tool_name": key, "version": schema.version})

    def list_tools(self) -> Iterable[ToolSchema]:
        return self._schemas.values()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.monotonic()
        tool = self._tools.get(invocation.tool_name)
        if not tool:
            return ToolResult(success=False, error="Unknown tool", correlation_id=invocation.correlation_id)

        schema = self._schemas.get(invocation.tool_name)
        if schema:
            # Auto-hydrate contextual identifiers when the LLM omits them.
            # Copy to avoid mutating the original payload shared across retries.
            params = dict(invocation.params or {})
            if (
                schema.requires_workshop
                and "workshop_id" not in params
                and invocation.workshop_id is not None
            ):
                params["workshop_id"] = invocation.workshop_id
            if (
                schema.requires_auth
                and "user_id" not in params
                and invocation.user_id is not None
            ):
                params["user_id"] = invocation.user_id

            # Targeted pre-normalization for common LLM argument aliasing mistakes
            # Do this BEFORE validation to avoid additionalProperties rejections
            # while still keeping schemas strict by removing consumed aliases.
            try:
                # Normalize for workshop_control.add_idea: accept aliases like
                # {idea: "..."}, {content: "..."}, {idea_text: "..."}, or
                # nested {idea: {text|content: "..."}} and map to {text: "..."}.
                if schema.full_name == "workshop_control.add_idea":
                    if "text" not in params:
                        aliases = ("idea", "content", "message", "idea_text")
                        extracted: str | None = None
                        for key in aliases:
                            if key in params:
                                val = params.get(key)
                                if isinstance(val, str) and val.strip():
                                    extracted = val.strip()
                                elif isinstance(val, dict):
                                    nested_val = val.get("text") or val.get("content")
                                    if isinstance(nested_val, str) and nested_val.strip():
                                        extracted = nested_val.strip()
                                # Remove the alias key to satisfy additionalProperties: false
                                params.pop(key, None)
                            if extracted:
                                break
                        if extracted:
                            params["text"] = extracted
            except Exception:
                # Best-effort normalization; continue with raw params if anything fails
                pass
            invocation.params = params

        try:
            if not tool.authorize(invocation):
                return ToolResult(success=False, error="Unauthorized", correlation_id=invocation.correlation_id)

            tool.validate_params(invocation.params)
            result = tool.execute(invocation.params)
        except Exception as exc:  # pragma: no cover - error path
            latency = (time.monotonic() - started) * 1000
            self._log(
                "error",
                "tool_execution_error",
                {
                    "tool_name": invocation.tool_name,
                    "correlation_id": invocation.correlation_id,
                    "workshop_id": invocation.workshop_id,
                    "user_id": invocation.user_id,
                    "error": str(exc),
                    "latency_ms": latency,
                },
            )
            return ToolResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                correlation_id=invocation.correlation_id,
            )

        result.latency_ms = (time.monotonic() - started) * 1000
        result.correlation_id = invocation.correlation_id

        self._log_invocation(tool, invocation, result)
        return result

    # ------------------------------------------------------------------
    def _log_invocation(self, tool: BaseTool, invocation: ToolInvocation, result: ToolResult) -> None:
        sanitized = tool.sanitized_params(invocation.params)
        payload = {
            "tool_name": invocation.tool_name,
            "correlation_id": invocation.correlation_id,
            "workshop_id": invocation.workshop_id,
            "user_id": invocation.user_id,
            "params": sanitized,
            "success": result.success,
            "rows_affected": result.rows_affected,
            "latency_ms": result.latency_ms,
            "error": result.error,
        }
        self._log("info", "tool_invocation", payload)
        log_tool_event("tool_invocation", payload)
        record_tool_metric(
            "tool_invocation",
            tool_name=invocation.tool_name,
            success=result.success,
            latency_ms=result.latency_ms,
            correlation_id=invocation.correlation_id,
            workshop_id=invocation.workshop_id,
        )

    def _log(self, level: str, message: str, extra: Dict[str, Any]) -> None:
        logger_obj = current_app.logger if has_app_context() else logger
        log_method = getattr(logger_obj, level, logger_obj.info)
        log_method(message, extra=extra)

    # Convenience for tests
    @property
    def tools(self) -> Dict[str, BaseTool]:
        return dict(self._tools)
