from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from flask import current_app
from jsonschema import ValidationError, validate

from app.models import Workshop, WorkshopParticipant, db

from .types import ToolInvocation, ToolResult, ToolSchema


class ToolExecutionError(RuntimeError):
    """Raised when a tool cannot complete successfully."""


class BaseTool(ABC):
    """Abstract base providing common validation and auth checks for tools."""

    @abstractmethod
    def get_schema(self) -> ToolSchema:
        """Return the JSON-schema contract for the tool."""

    @abstractmethod
    def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute tool logic. Implementations should return ``ToolResult``."""

    # ------------------------------------------------------------------
    # Helper hooks
    # ------------------------------------------------------------------
    def authorize(self, invocation: ToolInvocation) -> bool:
        """Baseline authorization check against workshop membership/role."""

        schema = self.get_schema()

        if not schema.requires_auth:
            return True

        if invocation.user_id is None:
            return False

        if schema.requires_workshop and invocation.workshop_id is None:
            return False

        if invocation.workshop_id is None:
            return True

        participant = (
            WorkshopParticipant.query
            .filter_by(workshop_id=invocation.workshop_id, user_id=invocation.user_id)
            .first()
        )
        if participant is None:
            return False

        if schema.allowed_roles and participant.role not in schema.allowed_roles:
            return False

        return True

    def ensure_workshop(self, workshop_id: Optional[int]) -> Workshop:
        if workshop_id is None:
            raise ToolExecutionError("Workshop context required")
        workshop = db.session.get(Workshop, workshop_id)
        if not workshop:
            raise ToolExecutionError("Workshop not found")
        return workshop

    def validate_params(self, params: Dict[str, Any]) -> None:
        """Validate invocation parameters against the declared JSON schema."""

        schema = self.get_schema()
        try:
            validate(params, schema.parameters)
        except ValidationError as exc:  # pragma: no cover - handled by callers in tests
            raise ToolExecutionError(f"Invalid parameters: {exc.message}") from exc

    def log_debug(self, message: str, **extra: Any) -> None:
        current_app.logger.debug(message, extra={"tool": extra})

    def log_warning(self, message: str, **extra: Any) -> None:
        current_app.logger.warning(message, extra={"tool": extra})

    def log_info(self, message: str, **extra: Any) -> None:
        current_app.logger.info(message, extra={"tool": extra})

    # ------------------------------------------------------------------
    # Utilities for subclasses
    # ------------------------------------------------------------------
    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return f"<redacted:{len(value)} chars>"
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value

    def sanitized_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {key: self._redact_value(value) for key, value in params.items()}
