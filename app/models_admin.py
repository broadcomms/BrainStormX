# app/models_admin.py
from datetime import datetime
from app.extensions import db

class AdminLog(db.Model):
    __tablename__ = "admin_logs"
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False) 
    action = db.Column(db.String(120), nullable=False, index=True)
    entity_type = db.Column(db.String(80), index=True)
    entity_id = db.Column(db.String(64), index=True)
    log_meta = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))  # Support IPv6
    user_agent = db.Column(db.Text)
    session_id = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    # Relationships
    actor = db.relationship("User", backref=db.backref("admin_actions", lazy=True))

    @classmethod
    def log_action(cls, actor_id, action, entity_type=None, entity_id=None, metadata=None, request=None):
        """Log admin action with context"""
        from flask import request as flask_request
        req = request or flask_request
        
        log_entry = cls(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=metadata,
            ip_address=req.remote_addr if req else None,
            user_agent=req.user_agent.string if req and req.user_agent else None
        )
        db.session.add(log_entry)
        db.session.commit()
        return log_entry

class UserSession(db.Model):
    """Track active user sessions for admin monitoring"""
    __tablename__ = "user_sessions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    session_token = db.Column(db.String(255), unique=True, nullable=False)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_activity = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    user = db.relationship("User", backref=db.backref("sessions", lazy=True))