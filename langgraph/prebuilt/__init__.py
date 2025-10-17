"""Stub implementations of langgraph.prebuilt APIs."""
from __future__ import annotations

from typing import Any, Dict


class _StubAgent:
    def __init__(self, *_, **__):
        pass

    def invoke(self, _state: Dict[str, Any]):
        return {"messages": [{"content": "Agent runtime is disabled in this environment."}]}


def create_react_agent(*args: Any, **kwargs: Any) -> _StubAgent:
    return _StubAgent()
