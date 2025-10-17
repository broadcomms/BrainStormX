"""Administrative dashboard data providers."""

from datetime import datetime, timedelta
from typing import Dict, List

from sqlalchemy import func

from app.extensions import db
from app.models import ChatMessage, User, Workshop, WorkspaceMember
from app.models_admin import AdminLog


class AdminDashboard:
    """Aggregate metrics for the administrative dashboard."""

    @staticmethod
    def get_system_metrics() -> Dict[str, Dict[str, int]]:
        now = datetime.utcnow()
        last_day = now - timedelta(days=1)

        return {
            "users": {
                "total": User.query.count(),
                "active_today": User.query.filter(User.updated_timestamp >= last_day).count(),
                "admins": User.query.filter_by(role="admin").count(),
                "managers": User.query.filter_by(role="manager").count(),
            },
            "workshops": {
                "total": Workshop.query.count(),
                "active": Workshop.query.filter(Workshop.status == "inprogress").count(),
                "completed_today": Workshop.query.filter(
                    Workshop.status == "completed",
                    Workshop.updated_at >= last_day,
                ).count(),
            },
            "activity": {
                "chat_messages_today": ChatMessage.query.filter(ChatMessage.timestamp >= last_day).count(),
                "workspaces": int(
                    db.session.query(func.count(func.distinct(WorkspaceMember.workspace_id))).scalar() or 0
                ),
            },
        }

    @staticmethod
    def recent_admin_logs(limit: int = 10) -> List[AdminLog]:
        return AdminLog.query.order_by(AdminLog.created_at.desc()).limit(limit).all()