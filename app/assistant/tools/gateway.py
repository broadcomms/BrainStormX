from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from threading import Lock
from typing import Iterable, List, Tuple

from flask import current_app, has_app_context
from app.config import Config

from app.assistant.context import AssistantContext
from app.assistant.schemas import AssistantToolCall, ToolExecutionResult

from . import ToolInvocation, ToolResult, ToolRegistry
from .observability import record_tool_metric


logger = logging.getLogger(__name__)


class ToolGateway:
    """Bridge between Assistant tool calls and the ToolRegistry with resiliency guards."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_workers: int | None = None,
        timeout_seconds: float | None = None,
        failure_threshold: int | None = None,
        circuit_reset_seconds: float | None = None,
    ) -> None:
        self.registry = registry
        self._lock = Lock()
        self.max_workers = self._resolve_int(max_workers, "TOOL_GATEWAY_MAX_WORKERS", 4, minimum=1)
        self.timeout_seconds = self._resolve_float(timeout_seconds, "TOOL_GATEWAY_TIMEOUT_SECONDS", 5.0, minimum=0.1)
        self.failure_threshold = self._resolve_int(
            failure_threshold,
            "TOOL_GATEWAY_FAILURE_THRESHOLD",
            3,
            minimum=1,
        )
        self.circuit_reset_seconds = self._resolve_float(
            circuit_reset_seconds,
            "TOOL_GATEWAY_CIRCUIT_RESET_SECONDS",
            60.0,
            minimum=1.0,
        )
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self._failure_counts: dict[str, int] = {}
        self._circuit_until: dict[str, float] = {}

    def execute(
        self,
        calls: Iterable[AssistantToolCall],
        context: AssistantContext | None,
        *,
        correlation_id: str | None = None,
    ) -> Tuple[List[ToolExecutionResult], List[ToolResult]]:
        results: List[ToolExecutionResult] = []
        raw: List[ToolResult] = []
        base_correlation = correlation_id or str(uuid.uuid4())

        workshop_id = context.workshop.id if context else None
        user_id = context.rbac.user_id if context and context.rbac else None

        notifier_requests = []
        notifier_available = "notifier.notify" in self.registry.tools

        for index, call in enumerate(calls):
            invocation = ToolInvocation(
                tool_name=call.name,
                params=call.args,
                correlation_id=f"{base_correlation}:{index}",
                workshop_id=workshop_id,
                user_id=user_id,
            )

            short_circuit = self._is_circuit_open(invocation.tool_name)
            if short_circuit:
                tool_result = ToolResult(
                    success=False,
                    error="Circuit breaker open",
                    correlation_id=invocation.correlation_id,
                )
                self._attach_gateway_metadata(tool_result, circuit_open=True)
                self._record_metric(
                    "tool_gateway_circuit_open",
                    tool_name=invocation.tool_name,
                    workshop_id=workshop_id,
                    correlation_id=invocation.correlation_id,
                )
                raw.append(tool_result)
                results.append(
                    ToolExecutionResult(
                        name=call.name,
                        success=tool_result.success,
                        output=None,
                        error=tool_result.error,
                        elapsed_ms=None,
                    )
                )
                continue

            start = time.perf_counter()
            future = self._submit_invocation(invocation)
            try:
                # Use a longer timeout for control-plane tools like begin/next/end
                call_timeout = self.timeout_seconds
                if invocation.tool_name.startswith("workshop_control."):
                    try:
                        call_timeout = float(getattr(Config, "TOOL_GATEWAY_CONTROL_TIMEOUT_SECONDS", self.timeout_seconds))
                    except Exception:
                        call_timeout = self.timeout_seconds
                tool_result = future.result(timeout=call_timeout)
            except FuturesTimeoutError:
                future.cancel()
                latency_ms = (time.perf_counter() - start) * 1000.0
                tool_result = ToolResult(
                    success=False,
                    error=f"Tool execution timeout ({call_timeout:.1f}s)",
                    latency_ms=latency_ms,
                    correlation_id=invocation.correlation_id,
                )
                self._attach_gateway_metadata(tool_result, timeout=True)
                self._register_failure(invocation.tool_name)
                self._log(
                    "warning",
                    "tool_gateway_timeout",
                    tool_name=invocation.tool_name,
                    correlation_id=invocation.correlation_id,
                    timeout_seconds=call_timeout,
                    workshop_id=workshop_id,
                )
                self._record_metric(
                    "tool_gateway_timeout",
                    tool_name=invocation.tool_name,
                    workshop_id=workshop_id,
                    correlation_id=invocation.correlation_id,
                    latency_ms=latency_ms,
                )
            except Exception as exc:  # pragma: no cover - defensive
                future.cancel()
                latency_ms = (time.perf_counter() - start) * 1000.0
                tool_result = ToolResult(
                    success=False,
                    error=str(exc),
                    latency_ms=latency_ms,
                    correlation_id=invocation.correlation_id,
                )
                self._attach_gateway_metadata(tool_result, exception=str(exc))
                self._register_failure(invocation.tool_name)
                self._log(
                    "error",
                    "tool_gateway_exception",
                    tool_name=invocation.tool_name,
                    correlation_id=invocation.correlation_id,
                    error=str(exc),
                    workshop_id=workshop_id,
                )
                self._record_metric(
                    "tool_gateway_exception",
                    tool_name=invocation.tool_name,
                    workshop_id=workshop_id,
                    correlation_id=invocation.correlation_id,
                )
            else:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                if not tool_result.latency_ms:
                    tool_result.latency_ms = elapsed_ms
                if tool_result.success:
                    self._register_success(invocation.tool_name)
                else:
                    self._register_failure(invocation.tool_name)
                self._attach_gateway_metadata(
                    tool_result,
                    failure_count=self._failure_counts.get(invocation.tool_name, 0),
                )
                if not tool_result.success:
                    self._log(
                        "warning",
                        "tool_gateway_tool_failure",
                        tool_name=invocation.tool_name,
                        correlation_id=invocation.correlation_id,
                        error=tool_result.error,
                        workshop_id=workshop_id,
                    )
                self._record_metric(
                    "tool_gateway_result",
                    tool_name=invocation.tool_name,
                    success=tool_result.success,
                    workshop_id=workshop_id,
                    correlation_id=invocation.correlation_id,
                    latency_ms=tool_result.latency_ms,
                )

            raw.append(tool_result)
            results.append(
                ToolExecutionResult(
                    name=call.name,
                    success=tool_result.success,
                    output=tool_result.data,
                    error=None if tool_result.success else tool_result.error,
                    elapsed_ms=int(tool_result.latency_ms) if tool_result.latency_ms is not None else None,
                )
            )
            notifier_payload = None
            if tool_result.success and isinstance(tool_result.metadata, dict):
                notifier_payload = tool_result.metadata.get("notifier")
            if (
                notifier_payload
                and notifier_payload.get("event_type")
                and workshop_id is not None
                and notifier_available
            ):
                notifier_requests.append(notifier_payload)

        for idx, payload in enumerate(notifier_requests):
            params = {
                "workshop_id": workshop_id,
                "event_type": payload.get("event_type"),
                "payload": payload.get("payload", {}),
            }
            invocation = ToolInvocation(
                tool_name="notifier.notify",
                params=params,
                correlation_id=f"{base_correlation}:notify:{idx}",
                workshop_id=workshop_id,
                user_id=user_id,
            )
            notify_result = self.registry.execute(invocation)
            raw.append(notify_result)
            self._record_metric(
                "notifier_dispatch",
                event_type=params["event_type"],
                success=notify_result.success,
                workshop_id=workshop_id,
                correlation_id=invocation.correlation_id,
            )
        return results, raw

    # ------------------------------------------------------------------
    def _register_success(self, tool_name: str) -> None:
        with self._lock:
            self._failure_counts[tool_name] = 0
            reopen = self._circuit_until.get(tool_name)
            if reopen is not None and reopen <= time.monotonic():
                self._circuit_until.pop(tool_name, None)

    def _register_failure(self, tool_name: str) -> None:
        with self._lock:
            count = self._failure_counts.get(tool_name, 0) + 1
            self._failure_counts[tool_name] = count
            if count >= self.failure_threshold:
                reopen_at = time.monotonic() + self.circuit_reset_seconds
                self._circuit_until[tool_name] = reopen_at
                self._log(
                    "warning",
                    "tool_gateway_circuit_opened",
                    tool_name=tool_name,
                    failure_count=count,
                    threshold=self.failure_threshold,
                    reopen_after=self.circuit_reset_seconds,
                )
                self._record_metric(
                    "tool_gateway_circuit_open",
                    tool_name=tool_name,
                    failure_count=count,
                    threshold=self.failure_threshold,
                    reopen_after=self.circuit_reset_seconds,
                )

    def _is_circuit_open(self, tool_name: str) -> bool:
        with self._lock:
            reopen_at = self._circuit_until.get(tool_name)
            if reopen_at is None:
                return False
            if reopen_at <= time.monotonic():
                self._circuit_until.pop(tool_name, None)
                self._failure_counts[tool_name] = 0
                self._record_metric(
                    "tool_gateway_circuit_reset",
                    tool_name=tool_name,
                    threshold=self.failure_threshold,
                )
                return False
            return True

    def _attach_gateway_metadata(self, tool_result: ToolResult, **metadata: object) -> None:
        if not isinstance(tool_result.metadata, dict):
            tool_result.metadata = {}
        gateway_meta = tool_result.metadata.setdefault("gateway", {})
        if isinstance(gateway_meta, dict):
            for key, value in metadata.items():
                if value is not None:
                    gateway_meta[key] = value

    def _resolve_float(
        self,
        override: float | None,
        config_key: str,
        default: float,
        *,
        minimum: float,
    ) -> float:
        if override is not None:
            return max(minimum, float(override))
        if has_app_context():
            value = current_app.config.get(config_key)
            if value is not None:
                try:
                    return max(minimum, float(value))
                except (TypeError, ValueError):
                    self._log("warning", "tool_gateway_config_invalid", key=config_key, value=value)
        return max(minimum, default)

    def _resolve_int(
        self,
        override: int | None,
        config_key: str,
        default: int,
        *,
        minimum: int,
    ) -> int:
        if override is not None:
            return max(minimum, int(override))
        if has_app_context():
            value = current_app.config.get(config_key)
            if value is not None:
                try:
                    return max(minimum, int(value))
                except (TypeError, ValueError):
                    self._log("warning", "tool_gateway_config_invalid", key=config_key, value=value)
        return max(minimum, default)

    def _log(self, level: str, message: str, **extra: object) -> None:
        logger_obj = current_app.logger if has_app_context() else logger
        log_method = getattr(logger_obj, level, logger_obj.info)
        log_method(message, extra={"tool_gateway": extra})

    def _record_metric(self, event: str, **payload: object) -> None:
        record_tool_metric(event, **payload)

    def shutdown(self, wait: bool = False) -> None:
        self.executor.shutdown(wait=wait)

    def _submit_invocation(self, invocation: ToolInvocation):
        if has_app_context():
            app_obj = current_app._get_current_object()  # type: ignore[attr-defined]

            def _runner():
                with app_obj.app_context():
                    return self.registry.execute(invocation)

            return self.executor.submit(_runner)
        return self.executor.submit(self.registry.execute, invocation)
