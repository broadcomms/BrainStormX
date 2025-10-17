# app/assistant/tools/notifier/catalog.py
from enum import Enum
from pydantic import BaseModel
from typing import Optional

class EventType(Enum):
    """Whitelisted event types"""
    IDEA_ADDED = "idea_added"
    CLUSTER_UPDATED = "cluster_updated"
    VOTE_CAST = "vote_cast"
    ACTION_CREATED = "action_item_created"
    PHASE_CHANGED = "phase_changed"
    TIMER_UPDATE = "timer_update"

# Event payload schemas
class IdeaAddedEvent(BaseModel):
    id: int
    text: str
    contributor_id: Optional[int]
    timestamp: str

class VoteCastEvent(BaseModel):
    cluster_id: int
    total_votes: int
    user_id: int

class ActionCreatedEvent(BaseModel):
    id: int
    title: str
    owner_id: Optional[int]
    priority: str

# Registry of event types to schemas
EVENT_SCHEMAS = {
    EventType.IDEA_ADDED: IdeaAddedEvent,
    EventType.VOTE_CAST: VoteCastEvent,
    EventType.ACTION_CREATED: ActionCreatedEvent
}
