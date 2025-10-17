from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from pydantic import BaseModel, ValidationError

from app.assistant.context import AssistantContext
from app.assistant.schemas import AssistantToolCall, ToolExecutionResult


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    args_schema: Optional[type[BaseModel]] = None
    roles_allowed: Set[str] = field(default_factory=lambda: {"organizer", "facilitator", "participant"})
    docs: str = ""
    allow_guest: bool = False

    def validate_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self.args_schema:
            return args
        try:
            model = self.args_schema(**args)
            return model.dict()
        except ValidationError as exc:  # pragma: no cover - defensive
            raise ValueError(f"Invalid args for {self.name}: {exc}")


class ToolExecutor:
    def __init__(self, catalog: Dict[str, ToolSpec]):
        self.catalog = catalog

    def execute(self, calls: Iterable[AssistantToolCall], ctx: AssistantContext) -> List[ToolExecutionResult]:
        results: List[ToolExecutionResult] = []
        for call in calls:
            spec = self.catalog.get(call.name)
            if not spec:
                results.append(ToolExecutionResult(name=call.name, success=False, output=None, error="unknown_tool"))
                continue
            if not self._is_authorized(spec, ctx):
                results.append(ToolExecutionResult(name=call.name, success=False, output=None, error="forbidden"))
                continue
            args = spec.validate_args(call.args)
            start = time.time()
            try:
                output = spec.fn(workshop_id=ctx.workshop.id, **args)
                elapsed = int((time.time() - start) * 1000)
                results.append(ToolExecutionResult(name=call.name, success=True, output=output, elapsed_ms=elapsed))
            except Exception as exc:  # pragma: no cover - runtime failures bubbled up
                elapsed = int((time.time() - start) * 1000)
                results.append(ToolExecutionResult(name=call.name, success=False, output=None, error=str(exc), elapsed_ms=elapsed))
        return results

    def _is_authorized(self, spec: ToolSpec, ctx: AssistantContext) -> bool:
        if spec.allow_guest:
            return True
        rbac = ctx.rbac
        if not rbac:
            return False
        if rbac.is_facilitator:
            return True
        return rbac.role in spec.roles_allowed
