"""Administrative REST API endpoints."""

from __future__ import annotations

from typing import cast

from flask import abort, jsonify, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import User, Workshop
from app.models_admin import AdminLog, UserSession

from . import admin_api_bp
from .dashboard import AdminDashboard
from .decorators import admin_required
from .health_monitor import HealthMonitor
from .user_management import UserManager
from .workshop_admin import WorkshopAdmin


@admin_api_bp.route("/metrics")
@login_required
@admin_required
def get_metrics():
    """Return current dashboard metrics and health snapshot."""

    metrics = AdminDashboard.get_system_metrics()
    health = HealthMonitor.get_system_health()
    recent_logs = [
        {
            "id": log.id,
            "actor_id": log.actor_id,
            "actor_name": (log.actor.display_name if getattr(log, "actor", None) else "System"),
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "metadata": getattr(log, "log_meta", None),
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in AdminDashboard.recent_admin_logs(limit=10)
    ]
    return jsonify({"metrics": metrics, "health": health, "recent_logs": recent_logs})


@admin_api_bp.route("/users/<int:user_id>/role", methods=["PUT", "PATCH"])
@login_required
@admin_required
def update_user_role(user_id: int):
    """Update a user's role."""

    data = request.get_json(silent=True) or {}
    new_role = data.get("role")
    if not new_role:
        abort(400, description="Missing role")

    try:
        user = cast(User, UserManager.get_user_with_metrics(user_id)["user"])
        UserManager.update_role(user, new_role)
        db.session.commit()
    except ValueError as exc:
        abort(400, description=str(exc))

    AdminLog.log_action(
        actor_id=current_user.user_id,
        action="user_role_change",
        entity_type="User",
        entity_id=str(user_id),
        metadata={"new_role": new_role},
    )
    return jsonify({"success": True, "user_id": user_id, "role": new_role})


@admin_api_bp.route("/workshops/<int:workshop_id>/snapshot")
@login_required
@admin_required
def workshop_snapshot(workshop_id: int):
    """Return a JSON snapshot of a workshop suitable for analytics."""

    workshop = Workshop.query.get_or_404(workshop_id)
    payload = WorkshopAdmin.export_workshop_data(workshop)
    return jsonify(payload)


@admin_api_bp.route("/workshops/<int:workshop_id>/export/pdf", methods=["POST"])
@login_required
@admin_required
def export_pdf(workshop_id: int):
    """Trigger a PDF export and log the action."""

    workshop = Workshop.query.get_or_404(workshop_id)
    pdf_bytes = WorkshopAdmin.export_workshop_pdf(workshop)

    AdminLog.log_action(
        actor_id=current_user.user_id,
        action="workshop_export",
        entity_type="Workshop",
        entity_id=str(workshop_id),
        metadata={"format": "pdf", "via_api": True},
    )

    return jsonify({"success": True, "size": len(pdf_bytes)})


@admin_api_bp.route("/sessions/<int:session_id>", methods=["DELETE"])
@login_required
@admin_required
def api_revoke_session(session_id: int):
    """Deactivate a user session."""

    session = UserSession.query.get_or_404(session_id)
    if not session.is_active:
        return jsonify({"success": True, "status": "already_inactive"})

    session.is_active = False
    db.session.commit()

    AdminLog.log_action(
        actor_id=current_user.user_id,
        action="session_revoked",
        entity_type="UserSession",
        entity_id=str(session_id),
        metadata={"user_id": session.user_id, "via_api": True},
    )
    return jsonify({"success": True, "status": "revoked"})