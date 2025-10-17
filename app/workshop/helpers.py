"""Utility helpers for facilitator and AI participant management."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import current_app

from app.extensions import db
from app.models import User, Workshop, WorkshopParticipant

_FACILITATOR_EMAIL = "facilitator@brainstormx.local"


def get_or_create_facilitator_user() -> User:
    """Return a stable facilitator user record, creating it if necessary."""
    user: Optional[User] = User.query.filter_by(email=_FACILITATOR_EMAIL).first()
    if user:
        if not (user.first_name or user.last_name):
            user.first_name = user.first_name or "AI Facilitator"
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        return user

    user = User()
    user.email = _FACILITATOR_EMAIL
    user.password = "!"  # Placeholder; account is system-managed only.
    user.first_name = "AI Facilitator"
    user.last_name = ""
    user.role = "user"
    db.session.add(user)
    db.session.commit()
    try:
        if current_app:
            current_app.logger.info("[Workshop] Created facilitator system user %s", _FACILITATOR_EMAIL)
    except Exception:
        pass
    return user


def ensure_ai_participant(workshop_id: int, *, role: str = "facilitator") -> WorkshopParticipant:
    """Guarantee a facilitator/AI participant row for the given workshop.

    Returns the participant instance (existing or newly created). The caller is
    responsible for committing the session after seeding related data.
    """
    workshop = db.session.get(Workshop, workshop_id)
    if not workshop:
        raise ValueError(f"Workshop {workshop_id} not found")

    fac_user = get_or_create_facilitator_user()
    participant: Optional[WorkshopParticipant] = (
        WorkshopParticipant.query
        .filter_by(workshop_id=workshop_id, user_id=fac_user.user_id)
        .first()
    )

    created = False
    updated = False
    desired_role = role or "facilitator"

    if participant is None:
        participant = WorkshopParticipant()
        participant.workshop_id = workshop_id
        participant.user_id = fac_user.user_id
        participant.role = desired_role
        participant.status = "accepted"
        participant.joined_timestamp = datetime.utcnow()
        db.session.add(participant)
        created = True
    else:
        if participant.status != "accepted":
            participant.status = "accepted"
            updated = True
        if participant.role != desired_role:
            participant.role = desired_role
            updated = True
        if participant.joined_timestamp is None:
            participant.joined_timestamp = datetime.utcnow()
            updated = True
        if updated:
            db.session.add(participant)

    if created or updated:
        try:
            db.session.flush()
        except Exception:
            db.session.rollback()
            raise
    else:
        try:
            db.session.flush([participant])
        except Exception:
            pass

    try:
        if created and current_app:
            current_app.logger.info(
                "[Workshop] Added facilitator participant %s to workshop %s",
                fac_user.email,
                workshop_id,
            )
    except Exception:
        pass

    return participant


# Backwards compatibility alias for legacy modules importing the private name.
_get_or_create_facilitator_user = get_or_create_facilitator_user
