# app/models_forum.py
from datetime import datetime
from typing import TYPE_CHECKING

from app.extensions import db

if TYPE_CHECKING:
    from flask_sqlalchemy.model import Model as _SQLAlchemyModel

    BaseModel = _SQLAlchemyModel
else:
    BaseModel = db.Model

class ForumCategory(BaseModel):
    __tablename__ = 'forum_categories'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    cluster_id = db.Column(db.Integer, index=True)  # optional link to IdeaCluster.id
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('workshop_id', 'cluster_id', name='uq_forum_cat_workshop_cluster'),
    )

class ForumTopic(BaseModel):
    __tablename__ = 'forum_topics'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey('forum_categories.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=True, index=True)
    title = db.Column(db.String(250), nullable=False)
    description = db.Column(db.Text)
    idea_id = db.Column(db.Integer, index=True)  # optional link back to BrainstormIdea.id
    pinned = db.Column(db.Boolean, default=False, nullable=False)
    locked = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('workshop_id', 'category_id', 'idea_id', name='uq_forum_topic_cat_idea'),
    )

class ForumPost(BaseModel):
    __tablename__ = 'forum_posts'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    topic_id = db.Column(db.Integer, db.ForeignKey('forum_topics.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    edited_at = db.Column(db.DateTime, nullable=True)

class ForumReply(BaseModel):
    __tablename__ = 'forum_replies'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('forum_posts.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    edited_at = db.Column(db.DateTime, nullable=True)

class ForumReaction(BaseModel):
    __tablename__ = 'forum_reactions'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    post_id = db.Column(db.Integer, db.ForeignKey('forum_posts.id'), nullable=True, index=True)
    reply_id = db.Column(db.Integer, db.ForeignKey('forum_replies.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    reaction = db.Column(db.String(32), nullable=False, default='like')  # like, love, clap, etc.
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('workshop_id', 'user_id', 'post_id', 'reply_id', 'reaction', name='uq_forum_reaction_uniq'),
        db.CheckConstraint('(post_id IS NOT NULL) OR (reply_id IS NOT NULL)', name='ck_forum_reaction_target'),
    )

class ForumAIAssist(db.Model):
    __tablename__ = "forum_ai_assist"

    id = db.Column(db.Integer, primary_key=True)
    forum_topic_id = db.Column(db.Integer, db.ForeignKey("forum_topics.id"), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # e.g. "devil_advocate", "mediator", "scribe"
    content = db.Column(db.Text, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship to forum topic
    topic = db.relationship("ForumTopic", backref=db.backref("ai_assists", lazy="dynamic"))
