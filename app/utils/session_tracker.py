"""Utilities for tracking user sessions in the database."""

from __future__ import annotations

from datetime import datetime
from secrets import token_urlsafe
from typing import Optional

from flask import current_app, request, session as flask_session
from flask_login import AnonymousUserMixin

from app.extensions import db
from app.models_admin import UserSession

_SESSION_TOKEN_KEY = "user_session_token"


def _now() -> datetime:
    return datetime.utcnow()


def _get_session_token() -> Optional[str]:
    token = flask_session.get(_SESSION_TOKEN_KEY)
    if isinstance(token, str) and token:
        return token
    return None


def _write_token(token: str) -> None:
    flask_session[_SESSION_TOKEN_KEY] = token
    flask_session.modified = True


def _clear_token() -> Optional[str]:
    token = flask_session.pop(_SESSION_TOKEN_KEY, None)
    if not isinstance(token, str):
        return None
    flask_session.modified = True
    return token


def _log_warning(message: str, **extra: object) -> None:
    try:
        current_app.logger.warning(message, extra=extra)  # type: ignore[arg-type]
    except Exception:
        pass


def _log_error(message: str, **extra: object) -> None:
    try:
        current_app.logger.error(message, extra=extra)  # type: ignore[arg-type]
    except Exception:
        pass


def _update_from_request(entry: UserSession) -> None:
    entry.last_activity = _now()
    entry.ip_address = request.remote_addr or entry.ip_address
    try:
        entry.user_agent = request.user_agent.string  # type: ignore[assignment]
    except Exception:
        pass


def begin_user_session(user) -> Optional[UserSession]:
    """Register or resume a database-backed session for the authenticated user."""

    if isinstance(user, AnonymousUserMixin):
        return None

    token = _get_session_token()
    entry: Optional[UserSession] = None
    if token:
        entry = UserSession.query.filter_by(session_token=token).first()

    if entry is None:
        token = token_urlsafe(32)
        _write_token(token)
        entry = UserSession(
            user_id=getattr(user, "user_id", None),
            session_token=token,
            created_at=_now(),
            last_activity=_now(),
            is_active=True,
        )
        _update_from_request(entry)
        db.session.add(entry)
    else:
        entry.user_id = getattr(user, "user_id", entry.user_id)
        entry.is_active = True
        _update_from_request(entry)

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        db.session.rollback()
        _log_error("user_session_begin_failed", error=str(exc))
        return None

    return entry


def touch_user_session(user) -> None:
    """Update the last_activity timestamp for the current request."""

    if isinstance(user, AnonymousUserMixin):
        return

    token = _get_session_token()
    entry: Optional[UserSession] = None
    if token:
        entry = UserSession.query.filter_by(session_token=token).first()

    if entry is None:
        begin_user_session(user)
        return

    entry.is_active = True
    _update_from_request(entry)

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        db.session.rollback()
        _log_warning("user_session_touch_failed", error=str(exc))


def end_user_session() -> None:
    """Mark the current session as inactive and clear the browser token."""

    token = _clear_token()
    if not token:
        return

    entry = UserSession.query.filter_by(session_token=token).first()
    if entry is None:
        return

    entry.is_active = False
    entry.last_activity = _now()

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        db.session.rollback()
        _log_warning("user_session_end_failed", error=str(exc))
