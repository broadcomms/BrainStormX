import json
import os
from datetime import datetime
from typing import Any, Dict, List

from app.config import Config


def _log_path() -> str:
    base = os.path.join(Config.INSTANCE_DIR, "logs")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "events.jsonl")


def log_event(event_type: str, payload: Dict[str, Any]):
    try:
        payload = dict(payload or {})
        payload["type"] = event_type
        payload["ts"] = datetime.utcnow().isoformat() + "Z"
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Best-effort logging only
        pass


def read_recent_events(limit: int = 20) -> List[Dict[str, Any]]:
    """Return up to last N telemetry events from JSONL log.

    Best-effort: tolerate missing/corrupt lines. Newest first.
    """
    path = _log_path()
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max(1, limit):]
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return out
    return out
