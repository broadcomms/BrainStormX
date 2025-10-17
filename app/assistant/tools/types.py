from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(slots=True)
class ToolInvocation:
    """Represents a single tool execution request."""

    tool_name: str
    params: Dict[str, Any]
    correlation_id: str
    workshop_id: Optional[int]
    user_id: Optional[int]
    timestamp: float = field(default_factory=time.time)


class ToolSchema(BaseModel):
    """Declarative schema describing a tool contract."""

    name: str
    namespace: str
    description: str
    parameters: Dict[str, Any]
    returns: Dict[str, Any]
    version: str = "1.0.0"
    requires_auth: bool = True
    requires_workshop: bool = True
    allowed_roles: Optional[set[str]] = None

    @field_validator("allowed_roles", mode="before")
    @classmethod
    def _coerce_roles(cls, value: Any) -> Optional[set[str]]:
        if value is None:
            return None
        if isinstance(value, (list, tuple, set)):
            return {str(item).lower() for item in value}
        return {str(value).lower()}

    @property
    def full_name(self) -> str:
        return f"{self.namespace}.{self.name}".strip(".")


class ToolResult(BaseModel):
    """Canonical response envelope for tool execution."""

    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    rows_affected: int = 0
    latency_ms: float = 0.0
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)
