from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import DefaultDict, Dict, Iterable, Union, cast

from flask import current_app

from app.config import TASK_SEQUENCE
from app.extensions import db, socketio
from app.models import BrainstormTask, Workshop

# --- In-memory storage for tracking ---
# { workshop_id: { user_id: last_submission_timestamp } }
WorkshopId = Union[int, str]
UserId = Union[int, str]

workshop_last_submission: DefaultDict[WorkshopId, Dict[UserId, datetime]] = defaultdict(dict)
# { workshop_id: { user_id: last_nudge_timestamp } }
workshop_last_nudge: DefaultDict[WorkshopId, Dict[UserId, datetime]] = defaultdict(dict)

# --- Configuration ---
NUDGE_THRESHOLD_SECONDS: int = 30  # Nudge if inactive for 60 seconds
NUDGE_COOLDOWN_SECONDS: int = 120  # Don't nudge the same user more than once every 120 seconds

def initialize_participant_tracking(workshop_id: WorkshopId, user_id: UserId) -> None:
    """Record when a participant joins."""
    now = datetime.utcnow()
    submission_map = workshop_last_submission[workshop_id]
    _ = workshop_last_nudge[workshop_id]  # Ensure defaultdict creates the entry

    # Set initial 'last submission' time to now; they'll be nudged if inactive
    submission_map[user_id] = now
    current_app.logger.debug(f"[Moderator] Initialized tracking for user {user_id} in workshop {workshop_id}")

def cleanup_participant_tracking(workshop_id: WorkshopId, user_id: UserId) -> None:
    """Remove participant data when they leave."""
    if workshop_id in workshop_last_submission and user_id in workshop_last_submission[workshop_id]:
        del workshop_last_submission[workshop_id][user_id]
    if workshop_id in workshop_last_nudge and user_id in workshop_last_nudge[workshop_id]:
        del workshop_last_nudge[workshop_id][user_id]
    current_app.logger.debug(f"[Moderator] Cleaned up tracking for user {user_id} in workshop {workshop_id}")

def clear_workshop_tracking(workshop_id: WorkshopId) -> None:
    """Clear all tracking data for a finished workshop."""
    if workshop_id in workshop_last_submission:
        del workshop_last_submission[workshop_id]
    if workshop_id in workshop_last_nudge:
        del workshop_last_nudge[workshop_id]
    current_app.logger.info(f"[Moderator] Cleared all tracking for workshop {workshop_id}")


def check_and_nudge(
    workshop_id: WorkshopId,
    submitter_user_id: UserId,
    current_participants_in_room: Iterable[UserId],
) -> None:
    """Checks inactivity and sends nudges via Socket.IO."""
    now = datetime.utcnow()
    workshop = db.session.get(Workshop, workshop_id)

    # --- Validation: Only nudge during active brainstorming ---
    if not workshop or workshop.status != 'inprogress':
        return
    current_task = cast(BrainstormTask | None, getattr(workshop, "current_task", None))
    if not current_task or workshop.current_task_index is None:
        return
    current_task_type = (
        TASK_SEQUENCE[workshop.current_task_index]
        if 0 <= workshop.current_task_index < len(TASK_SEQUENCE)
        else "unknown"
    )
    if current_task_type not in ["warm-up", "brainstorming"]:  # Only nudge during these phases
        current_app.logger.debug(f"[Moderator] Skipping nudge, current task type is {current_task_type}")
        return
    # ---------------------------------------------------------

    # Update submitter's last submission time
    submission_map = workshop_last_submission[workshop_id]
    nudge_map = workshop_last_nudge[workshop_id]
    submission_map[submitter_user_id] = now
    current_app.logger.debug(f"[Moderator] Updated last submission for user {submitter_user_id} in workshop {workshop_id}")

    # Check other participants
    for user_id in current_participants_in_room:
        if user_id == submitter_user_id:
            continue  # Don't nudge the person who just submitted

        last_submission = submission_map.get(user_id)
        last_nudge = nudge_map.get(user_id)

        if last_submission:
            time_since_submission = (now - last_submission).total_seconds()
            time_since_nudge = (now - last_nudge).total_seconds() if last_nudge else float('inf')

            if time_since_submission > NUDGE_THRESHOLD_SECONDS and time_since_nudge > NUDGE_COOLDOWN_SECONDS:
                # --- Emit nudge to specific user ---
                # Find the SID for the target user (requires _sid_registry access or modification)
                # For now, we assume a way to get the SID or emit to a user-specific room if implemented.
                # Simplified: Emitting to the main room, client JS needs to check if it's for them.
                # A better approach involves user-specific rooms or SID mapping.
                socketio.emit(
                    'moderator_nudge',
                    {'message': "Keep the ideas flowing!", 'target_user_id': user_id},
                    to=f'workshop_room_{workshop_id}'
                )
                nudge_map[user_id] = now
                current_app.logger.info(f"[Moderator] Nudged user {user_id} in workshop {workshop_id}")
