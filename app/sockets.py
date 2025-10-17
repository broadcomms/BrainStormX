# app/sockets.py
"""Compatibility shim for legacy `app.sockets` imports.

The original monolithic Socket.IO logic has moved to dedicated modules:
  * `app.sockets_core.core`  – core presence/chat/timers/workshop lifecycle
  * `app.sockets.video_conference_gateway` – video & media state
  * `app.sockets.transcription_gateway`    – speech‑to‑text / live transcript

This file intentionally keeps a very small surface:
  * Re‑export everything from `app.sockets_core.core` so old imports keep working
  * Import feature gateways for their side‑effect handler registration
  * Emit a deprecation warning at import time

Planned removal: once all call sites import the specific gateway or
`app.sockets_core.core` directly, this shim will be deleted.
"""

from __future__ import annotations

import warnings as _warnings

# Re-export core public API (handlers, emit helpers, etc.)
from app.sockets_core.core import *  # type: ignore  # noqa: F401,F403,E402

# Side-effect imports to register additional namespaces/handlers
from app.sockets import video_conference_gateway as video_conference_gateway  # noqa: F401,E402
from app.sockets import transcription_gateway as transcription_gateway  # noqa: F401,E402

_warnings.warn(
    "app.sockets is deprecated; import from app.sockets_core.core or the specific gateway module instead.",
    DeprecationWarning,
    stacklevel=2,
)

# Static export list kept intentionally minimal; core re-exports handled via * import above.
# Add additional names here only if the shim itself defines them (currently none).
__all__ = []
# --- ADDED: Generic Status Update Emitter ---
def emit_workshop_status_update(room: str, workshop_id: int, status: str):
    """Notifies clients of a general status change."""
    socketio.emit("workshop_status_update", {"workshop_id": workshop_id, "status": status}, to=room)
    current_app.logger.info(f"Emitted workshop_status_update ({status}) to {room}")