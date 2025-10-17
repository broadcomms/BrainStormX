from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import current_app
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    ActionItem,
    BrainstormIdea,
    BrainstormTask,
    CapturedDecision,
    DiscussionNote,
    Document,
    IdeaCluster,
    Workshop,
    WorkshopDocument,
    WorkshopParticipant,
)
from app.utils.data_aggregation import aggregate_pre_workshop_data


def _parse_iso_date(d: Optional[str]) -> Optional[date]:
    if not d:
        return None
    try:
        return date.fromisoformat(d)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(d.replace("Z", "+00:00")).date()
    except Exception:
        current_app.logger.warning("[skill_add_action_item] Invalid due_date '%s' ignored", d)
        return None


def _serialize_decision(row: CapturedDecision) -> Dict[str, Any]:
    return {
        "id": getattr(row, "id", None),
        "workshop_id": getattr(row, "workshop_id", None),
        "topic": getattr(row, "topic", None),
        "decision": getattr(row, "decision", None),
        "owner_user_id": getattr(row, "owner_user_id", None),
        "rationale": getattr(row, "rationale", None) or getattr(row, "rational", None),
        "status": getattr(row, "status", None),
        "created_at": getattr(row, "created_at", None).isoformat() if getattr(row, "created_at", None) else None,
    }


def _serialize_action_item(row: ActionItem) -> Dict[str, Any]:
    owner_participant = getattr(row, "owner_participant_id", None)
    return {
        "id": row.id,
        "workshop_id": row.workshop_id,
        "title": row.title,
        "owner_participant_id": owner_participant,
        "owner_user_id": getattr(row, "owner_user_id", None),
        "due_date": row.due_date.isoformat() if getattr(row, "due_date", None) else None,
        "success_metric": getattr(row, "success_metric", None),
        "status": getattr(row, "status", None),
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
    }


def skill_fetch_workshop_context(workshop_id: int) -> Dict[str, Any]:
    workshop = Workshop.query.get(workshop_id)
    if not workshop:
        raise ValueError(f"Workshop with id {workshop_id} not found")

    context: Dict[str, Any] = {
        "id": workshop.id,
        "title": workshop.title,
        "objective": getattr(workshop, "objective", None),
        "status": getattr(workshop, "status", None),
        "date_time": workshop.date_time.isoformat() if getattr(workshop, "date_time", None) else None,
        "duration": getattr(workshop, "duration", None),
        "created_by_id": getattr(workshop, "created_by_id", None),
        "created_at": workshop.created_at.isoformat() if getattr(workshop, "created_at", None) else None,
        "participants": [],
        "timers": {
            "current_task_id": getattr(workshop, "current_task_id", None),
            "current_phase": getattr(workshop, "current_phase", None),
            "current_task_remaining": getattr(workshop, "current_task_remaining", None),
        },
    }

    participants = (
        WorkshopParticipant.query.options(joinedload(WorkshopParticipant.user))
        .filter_by(workshop_id=workshop_id)
        .all()
    )
    for participant in participants:
        user = participant.user
        context["participants"].append(
            {
                "id": participant.id,
                "user_id": participant.user_id,
                "role": participant.role,
                "status": participant.status,
                "display_name": getattr(user, "display_name", None) or getattr(user, "email", "unknown"),
            }
        )

    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    context["pre_workshop_data"] = pre_workshop_data

    return context


def skill_fetch_phase_snapshot(workshop_id: int) -> Dict[str, Any]:
    def _latest_payload(task_type: str) -> Optional[Any]:
        record = (
            BrainstormTask.query.filter_by(workshop_id=workshop_id, task_type=task_type)
            .order_by(BrainstormTask.created_at.desc())
            .first()
        )
        if not record or not record.payload_json:
            return None
        try:
            return json.loads(record.payload_json)
        except Exception:
            return record.payload_json

    return {
        "framing": _latest_payload("framing"),
        "brainstorming": _latest_payload("brainstorming"),
        "clustering_voting": _latest_payload("clustering_voting"),
        "feasibility": _latest_payload("results_feasibility"),
        "prioritization": _latest_payload("results_prioritization"),
        "discussion": _latest_payload("results_discussion"),
        "action_plan": _latest_payload("results_action_plan"),
        "summary": _latest_payload("summary"),
    }


def skill_retrieve_workshop_phase(workshop_id: int) -> Dict[str, Any]:
    workshop = Workshop.query.get(workshop_id)
    if not workshop:
        raise ValueError(f"Workshop with id {workshop_id} not found")

    current_phase = getattr(workshop, "current_phase", None)
    current_task_id = getattr(workshop, "current_task_id", None)
    started_at = getattr(workshop, "current_task_started_at", None) or getattr(workshop, "current_task_start_time", None)
    remaining = getattr(workshop, "current_task_remaining", None)
    task_meta: Dict[str, Any] | None = None

    if current_task_id:
        task = db.session.get(BrainstormTask, current_task_id)
        if task:
            task_meta = {
                "id": task.id,
                "title": task.title,
                "task_type": task.task_type,
                "duration": task.duration,
                "status": task.status,
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "ended_at": task.ended_at.isoformat() if task.ended_at else None,
            }

    return {
        "phase": current_phase,
        "current_task_id": current_task_id,
        "task": task_meta,
        "timer": {
            "started_at": started_at.isoformat() if started_at else None,
            "remaining_seconds": remaining,
        },
    }


def skill_search_documents(workshop_id: int, query: str) -> List[Dict[str, Any]]:
    links = (
        WorkshopDocument.query
        .options(joinedload(WorkshopDocument.document))
        .filter(WorkshopDocument.workshop_id == workshop_id)
        .join(Document, WorkshopDocument.document_id == Document.id)
        .filter(Document.title.ilike(f"%{query}%") | Document.description.ilike(f"%{query}%"))
        .all()
    )

    out: List[Dict[str, Any]] = []
    for link in links[:8]:
        doc = link.document
        if not doc:
            continue
        out.append(
            {
                "document_id": doc.id,
                "title": doc.title,
                "summary": (doc.summary or doc.description or "")[:400],
                "file_path": doc.file_path,
            }
        )
    return out


def skill_list_decisions(workshop_id: int) -> List[Dict[str, Any]]:
    q = CapturedDecision.query.filter(CapturedDecision.workshop_id == workshop_id)
    if hasattr(CapturedDecision, "created_at"):
        q = q.order_by(desc(CapturedDecision.created_at))
    else:
        q = q.order_by(desc(CapturedDecision.id))
    rows = q.limit(20).all()
    return [_serialize_decision(r) for r in rows]


def skill_add_action_item(
    workshop_id: int,
    title: str,
    owner_user_id: Optional[int] = None,
    due_date: Optional[str] = None,
    metric: Optional[str] = None,
) -> Dict[str, Any]:
    title_clean = (title or "").strip()
    if not title_clean:
        raise ValueError("title is required")

    owner_participant_id: Optional[int] = None
    if owner_user_id is not None and owner_user_id != 0:
        participant = WorkshopParticipant.query.filter_by(
            workshop_id=workshop_id,
            user_id=owner_user_id,
        ).first()
        if not participant:
            raise ValueError(f"user {owner_user_id} is not part of this workshop")
        owner_participant_id = participant.id

    due_dt = _parse_iso_date(due_date)

    action_item = ActionItem(
        workshop_id=workshop_id,
        title=title_clean,
        owner_participant_id=owner_participant_id,
        due_date=due_dt,
        success_metric=metric or None,
    )
    db.session.add(action_item)
    db.session.flush()

    return _serialize_action_item(action_item)


def skill_summarize_transcripts(workshop_id: int, window_minutes: int = 10) -> Dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    notes = (
        DiscussionNote.query
        .filter(DiscussionNote.workshop_id == workshop_id)
        .filter(DiscussionNote.ts >= cutoff)
        .order_by(DiscussionNote.ts.desc())
        .limit(50)
        .all()
    )
    if not notes:
        return {
            "window_minutes": window_minutes,
            "highlights": [],
            "notable_participants": [],
            "summary": "No discussion notes captured in the selected timeframe.",
        }

    highlights = []
    seen_users: Dict[int, int] = {}
    for note in notes:
        if note.speaker_user_id:
            seen_users[note.speaker_user_id] = seen_users.get(note.speaker_user_id, 0) + 1
        snippet = (note.point or "").strip()
        if snippet:
            highlights.append(snippet[:240])
    highlights = highlights[:5]

    top_participants = sorted(seen_users.items(), key=lambda x: x[1], reverse=True)[:3]
    notable_participants = [user_id for user_id, _ in top_participants]

    summary = highlights[0] if highlights else "Captured discussions summarised."

    return {
        "window_minutes": window_minutes,
        "highlights": highlights,
        "notable_participants": notable_participants,
        "summary": summary,
    }


def skill_generate_devil_advocate(workshop_id: int, cluster_id: int) -> Dict[str, Any]:
    cluster = IdeaCluster.query.filter_by(id=cluster_id).first()
    if not cluster:
        raise ValueError("cluster not found")
    if not cluster.task or cluster.task.workshop_id != workshop_id:
        raise ValueError("cluster does not belong to this workshop")

    idea_samples = []
    ideas_query = cluster.ideas  # type: ignore[assignment]
    if hasattr(ideas_query, "limit"):
        for idea in ideas_query.limit(5):
            idea_samples.append((idea.content or "").strip())
    else:
        for idea in ideas_query[:5]:  # type: ignore[index]
            idea_samples.append((idea.content or "").strip())

    challenges: List[str] = []
    if not idea_samples:
        challenges.append("No supporting ideas recorded; validate this cluster's relevance before committing.")
    else:
        first = idea_samples[0]
        if len(first) > 120:
            first = first[:120] + "..."
        challenges.append(f"What assumptions underpin '{first}' and what happens if they fail?")
        challenges.append("Have we compared this approach against a baseline alternative for cost and effort?")
        challenges.append("What evidence is missing that would increase confidence in this direction?")

    return {
        "cluster": {
            "id": cluster.id,
            "name": cluster.name,
            "description": cluster.description,
            "idea_count": cluster.ideas.count() if hasattr(cluster.ideas, "count") else len(idea_samples),
        },
        "challenges": challenges,
    }


def skill_capture_decision(
    workshop_id: int,
    topic: str,
    decision: str,
    rationale: Optional[str] = None,
    owner_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    topic_clean = (topic or "").strip()
    if not topic_clean:
        raise ValueError("topic is required")
    decision_clean = (decision or "").strip()
    if not decision_clean:
        raise ValueError("decision is required")

    owner_user_id = owner_user_id or None
    record = CapturedDecision(
        workshop_id=workshop_id,
        topic=topic_clean,
        decision=decision_clean,
        rationale=(rationale or None),
        owner_user_id=owner_user_id,
        status="confirmed" if owner_user_id else "draft",
    )
    db.session.add(record)
    db.session.flush()
    return _serialize_decision(record)


def skill_render_whiteboard_snapshot(workshop_id: int, format: str = "image") -> Dict[str, Any]:
    clusters = (
        IdeaCluster.query
        .join(BrainstormTask, IdeaCluster.task_id == BrainstormTask.id)
        .filter(BrainstormTask.workshop_id == workshop_id)
        .order_by(IdeaCluster.updated_at.desc())
        .limit(12)
        .all()
    )
    out_clusters = []
    for cluster in clusters:
        ideas = (
            BrainstormIdea.query
            .filter(BrainstormIdea.cluster_id == cluster.id)
            .order_by(BrainstormIdea.timestamp.asc())
            .limit(12)
            .all()
        )
        out_clusters.append(
            {
                "id": cluster.id,
                "title": cluster.name,
                "description": cluster.description,
                "idea_samples": [idea.content[:140] for idea in ideas if idea.content],
                "color": getattr(cluster, "color", None),
            }
        )
    return {
        "format": format,
        "clusters": out_clusters,
        "generated_at": datetime.utcnow().isoformat(),
    }


def skill_explain_chart(workshop_id: int, chart_id: str) -> Dict[str, Any]:
    task = (
        BrainstormTask.query
        .filter(BrainstormTask.workshop_id == workshop_id)
        .filter(BrainstormTask.task_type.in_(["results_feasibility", "results_prioritization", "summary"]))
        .order_by(BrainstormTask.created_at.desc())
        .first()
    )
    chart_meta: Optional[Dict[str, Any]] = None
    if task and task.payload_json:
        try:
            payload = json.loads(task.payload_json)
            charts = payload.get("charts") if isinstance(payload, dict) else None
            if isinstance(charts, dict):
                chart_meta = charts.get(chart_id)
            elif isinstance(charts, list):
                chart_meta = next((c for c in charts if c.get("id") == chart_id), None)
        except Exception:
            chart_meta = None
    return {
        "chart_id": chart_id,
        "chart": chart_meta,
        "status": "ok" if chart_meta else "not_found",
    }
