"""
app/service/routes/actions.py

LLM-backed generation and import of Action Items from workshop context.
Mirrors the architecture used by other service modules (e.g., summary, brainstorming).
"""
import json
from datetime import datetime
from typing import List, Dict, Any, Tuple
from flask import current_app

from app.extensions import db
from app.models import (
    Workshop,
    BrainstormTask,
    BrainstormIdea,
    IdeaCluster,
    IdeaVote,
    ChatMessage,
    WorkshopParticipant,
    ActionItem,
)
from app.config import Config
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import get_pre_workshop_context_json
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from app.models import WorkshopParticipant as WP
from app.utils.llm_bedrock import get_chat_llm
from pydantic import SecretStr
from langchain_core.prompts import PromptTemplate


def _participants_snapshot(workshop_id: int) -> List[Dict[str, Any]]:
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop_id).all()
    out = []
    for p in participants:
        u = getattr(p, 'user', None)
        out.append({
            "participant_id": p.id,
            "name": getattr(u, 'first_name', None) or (getattr(u, 'email', '') or '').split('@')[0],
            "email": getattr(u, 'email', None),
        })
    return out


def _collect_activity_summary(workshop_id: int) -> str:
    """Collect a concise activity summary to improve action item quality."""
    pre = get_pre_workshop_context_json(workshop_id)
    ideas = BrainstormIdea.query.filter(BrainstormIdea.task.has(workshop_id=workshop_id)).all()
    clusters_with_counts = (
        db.session.query(IdeaCluster, func.count(IdeaVote.id).label('vote_count'))
        .outerjoin(IdeaVote, IdeaCluster.id == IdeaVote.cluster_id)
        .filter(IdeaCluster.task.has(workshop_id=workshop_id))
        .group_by(IdeaCluster.id)
        .all()
    )
    chat_messages = (
        ChatMessage.query
        .filter_by(workshop_id=workshop_id)
        .order_by(ChatMessage.timestamp)
        .all()
    )

    parts = [pre, "\n\n-- Activity Snapshot --\n"]
    if ideas:
        parts.append(f"Ideas ({len(ideas)}):\n" + "\n".join([f"- {i.content[:120]}" for i in ideas[:12]]))
    if clusters_with_counts:
        parts.append("\nClusters:\n" + "\n".join([f"- {c.name} (votes: {vc})" for c, vc in clusters_with_counts]))
    if chat_messages:
        last = chat_messages[-5:]
        parts.append("\nChat snippets:\n" + "\n".join([f"- {m.username or 'User'}: {m.message[:80]}" for m in last]))
    return "\n".join([p for p in parts if p])


def generate_action_items_text(workshop_id: int) -> Tuple[str, int]:
    """Call LLM to propose action items in JSON format."""
    current_app.logger.debug(f"[Actions] Generating action items for workshop {workshop_id}")
    context = _collect_activity_summary(workshop_id)
    participants = _participants_snapshot(workshop_id)
    if not context:
        return "Could not generate action items: Workshop data unavailable.", 500

    prompt_template = """
You are an expert facilitator. Based on the workshop context and participants, propose concrete action items.

Workshop Context:
{context}

Participants (for assignment reference):
{participants}

Instructions:
- Return a single JSON object with the key `items`, a list of action items.
- Each action item MUST include: title (string), description (string or null), owner (string name or email, try to resolve to the listed participants), due_date (YYYY-MM-DD or null), status (one of: todo, in_progress, done, blocked).
- Prioritize clarity and ownership. Choose reasonable due dates when implied by context.

Output JSON schema:
{{
  "items": [
    {{"title": "...", "description": "...", "owner": "Alice" or "alice@example.com", "due_date": "YYYY-MM-DD" or null, "status": "todo|in_progress|done|blocked"}}
  ]
}}

Respond with ONLY the JSON object.
"""

    llm = get_chat_llm(
        model_kwargs={
            "temperature": 0.6,
            "max_tokens": 800,
        }
    )
    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | llm
    try:
        raw = chain.invoke({
            "context": context,
            "participants": json.dumps(participants, ensure_ascii=False),
        })
        current_app.logger.debug(f"[Actions] Raw LLM output for {workshop_id}: {raw}")

        # Normalize AIMessage/dict to plain string
        def _to_text(val):
            try:
                if val is None:
                    return ""
                if isinstance(val, str):
                    return val
                if hasattr(val, "content"):
                    return val.content
                if isinstance(val, dict):
                    if "content" in val:
                        return val.get("content")
                    return json.dumps(val)
                return str(val)
            except Exception:
                return str(val)

        text = _to_text(raw) or ""
        return text, 200
    except Exception as e:
        current_app.logger.error(f"[Actions] LLM error for workshop {workshop_id}: {e}", exc_info=True)
        return f"Error generating action items: {e}", 500


def parse_action_items(raw_text: Any) -> List[Dict[str, Any]]:
    # Normalize to string first
    def _to_text(val):
        try:
            if val is None:
                return ""
            if isinstance(val, str):
                return val
            if hasattr(val, "content"):
                return val.content
            if isinstance(val, dict):
                if "content" in val:
                    return val.get("content")
                return json.dumps(val)
            return str(val)
        except Exception:
            return str(val)

    text = _to_text(raw_text) or ""
    block = extract_json_block(text) or text
    try:
        data = json.loads(block)
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _match_owner(workshop_id: int, owner_field: str | None) -> int | None:
    if not owner_field:
        return None
    needle = (owner_field or "").strip().lower()
    parts = WorkshopParticipant.query.filter_by(workshop_id=workshop_id).all()
    for p in parts:
        u = getattr(p, 'user', None)
        name = (getattr(u, 'first_name', None) or '').strip().lower()
        email = (getattr(u, 'email', None) or '').strip().lower()
        handle = (email.split('@')[0] if '@' in email else email)
        if needle in (name, email, handle):
            return p.id
    return None


def create_action_items_from_list(workshop_id: int, items: List[Dict[str, Any]], *, commit: bool = True) -> List[ActionItem]:
    created: List[ActionItem] = []
    for it in items:
        try:
            title = (it.get('title') or '').strip()
            if not title:
                continue
            ai = ActionItem()
            ai.workshop_id = workshop_id
            ai.title = title
            ai.description = (it.get('description') or None)
            ai.status = (it.get('status') or 'todo').strip().lower()[:50]
            # Owner matching
            ai.owner_participant_id = _match_owner(workshop_id, it.get('owner'))
            # Due date
            due = it.get('due_date')
            if due:
                try:
                    ai.due_date = datetime.strptime(due, "%Y-%m-%d").date()
                except Exception:
                    ai.due_date = None
            db.session.add(ai)
            created.append(ai)
        except Exception as e:
            current_app.logger.warning(f"[Actions] Skipped invalid item for workshop {workshop_id}: {e}")
    if commit and created:
        db.session.commit()
    return created


def import_action_items(workshop_id: int) -> Tuple[Dict[str, Any], int]:
    raw, code = generate_action_items_text(workshop_id)
    if code != 200:
        return {"error": raw}, code
    items = parse_action_items(raw)
    if not items:
        return {"error": "No action items found in model output."}, 400
    try:
        # Replace mode: remove existing, then insert generated set atomically
        db.session.query(ActionItem).filter_by(workshop_id=workshop_id).delete(synchronize_session=False)
        db.session.flush()
        created = create_action_items_from_list(workshop_id, items, commit=False)
        db.session.commit()
        return {
            "success": True,
            "created": [
                {
                    "id": ai.id,
                    "title": ai.title,
                    "description": ai.description,
                    "status": ai.status,
                    "due_date": ai.due_date.isoformat() if ai.due_date else None,
                    "owner_participant_id": ai.owner_participant_id,
                }
                for ai in created
            ],
            "count": len(created),
    }, 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[Actions] Import failed for workshop {workshop_id}: {e}", exc_info=True)
        return {"error": "Failed to persist action items."}, 500
