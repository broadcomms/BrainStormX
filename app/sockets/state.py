"""
Lightweight runtime state shared between socket handlers.

Currently used to track whether the AI Facilitator TTS is actively playing for a
workshop so we can suppress human STT finals during playback and avoid duplicate
transcripts.
"""
from __future__ import annotations

import time
from typing import Optional, Dict, Any

# workshop_id -> { 'active': bool, 'task_id': Optional[int], 'ts': float }
_facilitator_playback: Dict[int, Dict[str, Any]] = {}

# Consider facilitator speaking "active" if last heartbeat within this many seconds
_TTL_SECONDS = 60 * 20  # 20 minutes safety window


def set_facilitator_playback(workshop_id: int, *, active: bool, task_id: Optional[int] = None) -> None:
    now = time.time()
    if active:
        _facilitator_playback[int(workshop_id)] = { 'active': True, 'task_id': int(task_id) if task_id is not None else None, 'ts': now }
    else:
        # Mark inactive but keep a short-lived record to handle racey events
        _facilitator_playback[int(workshop_id)] = { 'active': False, 'task_id': int(task_id) if task_id is not None else None, 'ts': now }


def touch_facilitator_playback(workshop_id: int) -> None:
    d = _facilitator_playback.get(int(workshop_id))
    if d is not None:
        d['ts'] = time.time()


def clear_facilitator_playback(workshop_id: int) -> None:
    _facilitator_playback.pop(int(workshop_id), None)


def is_facilitator_playing(workshop_id: int) -> bool:
    d = _facilitator_playback.get(int(workshop_id))
    if not d:
        return False
    # TTL expiry
    if (time.time() - float(d.get('ts') or 0)) > _TTL_SECONDS:
        try:
            _facilitator_playback.pop(int(workshop_id), None)
        except Exception:
            pass
        return False
    return bool(d.get('active'))


def current_facilitator_task_id(workshop_id: int) -> Optional[int]:
    d = _facilitator_playback.get(int(workshop_id))
    if not d:
        return None
    return d.get('task_id')
