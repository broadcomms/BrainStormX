"""Compatibility shim for legacy imports.

This module re-exports ToolGateway from the newer location under
`app.assistant.tools.gateway` so older code and tests that import
`app.assistant.gateway` continue to function.
"""

from app.assistant.tools.gateway import ToolGateway

__all__ = ["ToolGateway"]
