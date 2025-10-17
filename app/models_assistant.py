from datetime import datetime

from app.extensions import db


class ChatThread(db.Model):
    __tablename__ = "chat_threads"

    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True, index=True)
    title = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)

    turns = db.relationship(
        "ChatTurn",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ChatTurn.created_at",
        lazy="selectin",
    )


class ChatTurn(db.Model):
    __tablename__ = "chat_turns"

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("chat_threads.id"), nullable=False, index=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False, index=True)
    role = db.Column(db.String(20))
    persona = db.Column(db.String(32), nullable=True)
    content = db.Column(db.Text)
    json_payload = db.Column(db.Text)
    plan_json = db.Column(db.Text)
    composed_json = db.Column(db.Text)
    tool_name = db.Column(db.String(100))
    tool_count = db.Column(db.Integer, default=0)
    latency_ms = db.Column(db.Integer, nullable=True)
    tool_latency_ms = db.Column(db.Integer, nullable=True)
    token_usage = db.Column(db.Integer, nullable=True)
    error_code = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True)

    thread = db.relationship("ChatThread", back_populates="turns")
    feedback_entries = db.relationship(
        "AssistantMessageFeedback",
        back_populates="turn",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class AssistantCitation(db.Model):
    __tablename__ = "assistant_citations"

    id = db.Column(db.Integer, primary_key=True)
    turn_id = db.Column(db.Integer, db.ForeignKey("chat_turns.id"), nullable=False, index=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=True)
    source_type = db.Column(db.String(50))
    source_ref = db.Column(db.String(200))
    snippet_hash = db.Column(db.String(64))
    start_char = db.Column(db.Integer)
    end_char = db.Column(db.Integer)

    turn = db.relationship("ChatTurn", backref=db.backref("citations", lazy="selectin"))


class AssistantMessageFeedback(db.Model):
    __tablename__ = "assistant_message_feedback"

    id = db.Column(db.Integer, primary_key=True)
    turn_id = db.Column(db.Integer, db.ForeignKey("chat_turns.id"), nullable=False, index=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True)
    rating = db.Column(db.String(8), nullable=False)  # up, down, flag
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    turn = db.relationship("ChatTurn", back_populates="feedback_entries")
