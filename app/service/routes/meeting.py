# app/service/routes/meeting.py
"""Meeting task payload generator.
Creates a simple meeting shell task that drives the live room to a meeting layout.
"""
from __future__ import annotations

import json
from datetime import datetime

from flask import current_app

from app.extensions import db
from app.models import BrainstormTask, Workshop
from app.tasks.registry import TASK_REGISTRY
from app.utils.value_parsing import safe_int


def get_meeting_payload(workshop_id: int, phase_context: str | None = None):
    """Create a Meeting task DB row and return its payload.
    The meeting task is a lightweight orchestrator: it primarily toggles the room UI.
    """
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return "Workshop not found", 404

    # Base payload with sensible defaults
    title = "Team Meeting"
    task_description = "General meeting session. Use the conference, chat and transcript as needed."
    instructions = "Discuss agenda topics. Raise hand to speak. The organizer can manage the flow."
    duration = safe_int(TASK_REGISTRY.get("meeting", {}).get("default_duration", 3600), default=3600)

    payload = {
        "title": title,
        "task_type": "meeting",
        "task_description": task_description,
        "instructions": instructions,
        "task_duration": duration,
        "narration": "Let's settle into our meeting. We'll walk through our agenda, share updates, and capture any actions.",
        "tts_script": "We're now in our meeting segment. We'll take a few minutes to share quick updates, discuss any blockers, and align on next steps. Feel free to raise your hand or post in chat. I'll keep an eye on time and make sure we capture any actions before we move on.",
        "tts_read_time_seconds": 45,
        "phase_context": phase_context or "",
    }

    # Create DB record
    task = BrainstormTask()
    task.workshop_id = workshop_id
    task.task_type = "meeting"
    task.title = payload["title"]
    task.description = payload.get("task_description")
    task.duration = safe_int(payload.get("task_duration", duration), default=duration)
    task.status = "pending"
    payload_str = json.dumps(payload)
    task.prompt = payload_str
    task.payload_json = payload_str
    db.session.add(task)
    db.session.flush()

    payload["task_id"] = task.id
    current_app.logger.info(f"[Meeting] Created task {task.id} for workshop {workshop_id}")
    return payload
