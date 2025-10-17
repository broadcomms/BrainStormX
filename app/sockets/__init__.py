"""Feature Socket.IO gateways (video conference, transcription) plus
re‑exports of legacy core emitters for backward compatibility.

Historically, the project exposed utility emitter functions and
presence registries from a flat module `app.sockets`. We refactored
core logic into `app.sockets_core` and feature‑scoped gateways into
this package directory (`app/sockets/`). Some parts of the codebase
(`workshop/routes.py`, etc.) still import symbols like
`emit_workshop_stopped` from `app.sockets`. To avoid a large, risky
search/replace, we re‑export the needed legacy symbols here.
"""

from . import transcription_gateway  # noqa: F401
from . import video_conference_gateway  # noqa: F401
from . import tts_gateway  # noqa: F401

# Re‑export legacy core functions/registries so existing imports keep working.
try:  # Defensive: only import if core is present
    from app.sockets_core.core import (  # type: ignore
        emit_workshop_stopped,
        emit_workshop_paused,
        emit_workshop_resumed,
        emit_warm_up_start,
        emit_task_ready,
        _room_presence,
        _sid_registry,
        _broadcast_participant_list,
    )
except Exception:  # pragma: no cover - fail silently; routes will error making issue visible
    pass

__all__ = [
    "transcription_gateway",
    "video_conference_gateway",
    "tts_gateway",
    # Legacy exports
    "emit_workshop_stopped",
    "emit_workshop_paused",
    "emit_workshop_resumed",
    "emit_warm_up_start",
    "emit_task_ready",
    "_room_presence",
    "_sid_registry",
    "_broadcast_participant_list",
]
