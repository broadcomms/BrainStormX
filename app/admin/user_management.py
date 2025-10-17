"""Administrative user management utilities."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

from flask_login import current_user
from passlib.hash import bcrypt
from sqlalchemy import or_

from app.extensions import db
from app.models import User, Workshop, WorkshopParticipant, WorkspaceMember
from app.models_admin import AdminLog


class UserManager:
    """Encapsulate CRUD operations for admin user management."""

    @staticmethod
    def paginate_users(page: int = 1, per_page: int = 25, search: Optional[str] = None):
        query = User.query
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    User.email.ilike(like),
                    User.first_name.ilike(like),
                    User.last_name.ilike(like),
                    User.username.ilike(like),
                )
            )
        return query.order_by(User.created_timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def get_user_with_metrics(user_id: int) -> Dict[str, object]:
        user = User.query.get_or_404(user_id)
        metrics = {
            "workspaces": WorkspaceMember.query.filter_by(user_id=user_id, status="active").count(),
            "workshops_created": Workshop.query.filter_by(created_by_id=user_id).count(),
            "workshops_participated": WorkshopParticipant.query.filter_by(user_id=user_id).count(),
            "last_activity": user.updated_timestamp,
        }
        return {"user": user, "metrics": metrics}

    @staticmethod
    def create_user(data: Dict[str, object]) -> User:
        email = str(data.get("email", "")).strip().lower()
        if not email:
            raise ValueError("Email is required.")
        if User.query.filter_by(email=email).first():
            raise ValueError("A user with that email already exists.")

        raw_password = str(data.get("password", ""))
        if len(raw_password) < 8:
            raise ValueError("Password must be at least 8 characters long.")

        user = User()
        user.email = email
        user.password = bcrypt.hash(raw_password)
        user.role = data.get("role", "user")
        user.first_name = data.get("first_name") or None
        user.last_name = data.get("last_name") or None
        user.username = data.get("username") or None
        user.job_title = data.get("job_title") or None
        user.organization = data.get("organization") or None
        user.email_verified = bool(data.get("email_verified", False))
        user.is_public_profile = bool(data.get("is_public_profile", False))
        db.session.add(user)
        db.session.commit()

        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="user_created",
            entity_type="User",
            entity_id=str(user.user_id),
            metadata={"email": user.email, "role": user.role},
        )
        return user

    @staticmethod
    def update_user(user: User, data: Dict[str, object]) -> User:
        old_snapshot = {"role": user.role, "email_verified": user.email_verified}

        for attr in ["first_name", "last_name", "username", "job_title", "organization"]:
            if attr in data:
                setattr(user, attr, data[attr] or None)

        if "role" in data:
            UserManager.update_role(user, str(data["role"]))
        if "email_verified" in data:
            user.email_verified = bool(data["email_verified"])
        if "is_public_profile" in data:
            user.is_public_profile = bool(data["is_public_profile"])

        user.updated_timestamp = datetime.utcnow()
        db.session.commit()

        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="user_updated",
            entity_type="User",
            entity_id=str(user.user_id),
            metadata={
                "before": old_snapshot,
                "after": {
                    "role": user.role,
                    "email_verified": user.email_verified,
                },
            },
        )
        return user

    @staticmethod
    def update_role(user: User, new_role: str) -> None:
        if new_role not in {"admin", "manager", "user"}:
            raise ValueError("Invalid role")
        user.role = new_role

    @staticmethod
    def delete_user(user: User) -> None:
        user_id = user.user_id
        db.session.delete(user)
        db.session.commit()
        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="user_deleted",
            entity_type="User",
            entity_id=str(user_id),
        )