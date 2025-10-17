# app/service/routes/discussion.py
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from flask import Blueprint, jsonify, request, current_app, session
from flask_login import login_required, current_user
from sqlalchemy import func

from app.config import Config
from app.extensions import db, socketio
from app.models import (
    BrainstormIdea,
    BrainstormTask,
    CapturedDecision,
    ChatMessage,
    DiscussionNote,
    DiscussionRun,
    DiscussionSettings,
    IdeaCluster,
    IdeaVote,
    Transcript,
    Workshop,
    WorkshopParticipant,
    ActionItem,
)
from app.models_forum import ForumAIAssist, ForumCategory, ForumTopic, ForumPost, ForumReply
from app.forum.service import seed_forum_from_results
from app.utils.json_utils import extract_json_block
from app.utils.llm_bedrock import get_chat_llm

from app.service.discussion_prompt import (
    Mode,
    get_mode_contract,
    build_prompt_template,
)

DISCUSSION_TASK_DURATION = 900
DEFAULT_MEDIATOR_INTERVAL = 300
DEFAULT_SCRIBE_INTERVAL = 240

NOTE_ORIGIN_BY_MODE: Dict[Mode, str] = {
    "initial": "ai_initial",
    "devil_advocate": "ai_devil_advocate",
    "mediator": "ai_mediator",
    "scribe": "ai_scribe",
}

DECISION_STATUS_BY_MODE: Dict[Mode, str] = {
    "initial": "draft",
    "mediator": "proposed",
}


discussion_bp = Blueprint("discussion_bp", __name__)
_LOCKS: Dict[int, threading.Lock] = {}
_LOCKS_MUTEX = threading.Lock()


class DiscussionStateError(RuntimeError):
    """Raised when the discussion state prevents executing a mode."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _lock_for(workshop_id: int) -> threading.Lock:
    with _LOCKS_MUTEX:
        return _LOCKS.setdefault(workshop_id, threading.Lock())


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return json.dumps(str(obj), ensure_ascii=False)


def _acting_user_id() -> Optional[int]:
    try:
        if session:
            raw_user_id = session.get("_user_id")  # type: ignore[attr-defined]
            if raw_user_id is not None:
                return int(raw_user_id)
    except Exception:
        pass
    try:
        if current_user.is_authenticated:  # type: ignore[attr-defined]
            return int(current_user.user_id)  # type: ignore[attr-defined]
    except Exception:
        return None
    return None


def _user_in_workshop(workshop_id: int, user_id: int) -> bool:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return False
    try:
        if int(ws.created_by_id) == int(user_id):
            return True
    except Exception:
        if ws.created_by_id == user_id:
            return True
    exists = (
        db.session.query(WorkshopParticipant.id)
        .filter(
            WorkshopParticipant.workshop_id == int(workshop_id),
            WorkshopParticipant.user_id == int(user_id),
        )
        .first()
    )
    return exists is not None


def _is_organizer(workshop_id: int, user_id: int) -> bool:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return False
    try:
        if int(ws.created_by_id) == int(user_id):
            return True
    except Exception:
        if ws.created_by_id == user_id:
            return True
    try:
        part = (
            db.session.query(WorkshopParticipant.id)
            .filter(
                WorkshopParticipant.workshop_id == workshop_id,
                WorkshopParticipant.user_id == user_id,
                WorkshopParticipant.role == "organizer",
            )
            .first()
        )
        return part is not None
    except Exception:
        return False


def _require_access(workshop_id: int, *, organizer: bool = False) -> Tuple[Optional[int], Optional[Tuple[str, int]]]:
    user_id = _acting_user_id()
    if not user_id:
        return None, ("Authentication required", 401)
    if organizer:
        if not _is_organizer(workshop_id, user_id):
            return user_id, ("Organizer privileges required", 403)
    else:
        if not _user_in_workshop(workshop_id, user_id):
            return user_id, ("Access to workshop denied", 403)
    return user_id, None


def _latest_payload(ws_id: int, types: List[str]) -> Optional[Dict[str, Any]]:
    task = (
        BrainstormTask.query
        .filter(BrainstormTask.workshop_id == ws_id, BrainstormTask.task_type.in_(types))
        .order_by(BrainstormTask.created_at.desc())
        .first()
    )
    if not task or not task.payload_json:
        return None
    try:
        return json.loads(task.payload_json)
    except Exception:
        return None


def _clusters(ws_id: int) -> List[Dict[str, Any]]:
    task = (
        BrainstormTask.query
        .filter_by(workshop_id=ws_id, task_type="clustering_voting")
        .order_by(BrainstormTask.created_at.desc())
        .first()
    )
    if not task:
        return []
    items = IdeaCluster.query.filter_by(task_id=task.id).all()
    out: List[Dict[str, Any]] = []
    for cluster in items:
        votes = IdeaVote.query.filter_by(cluster_id=cluster.id).count()
        ideas = BrainstormIdea.query.filter_by(cluster_id=cluster.id).all()
        out.append(
            {
                "cluster_id": cluster.id,
                "title": cluster.name or f"Cluster {cluster.id}",
                "description": cluster.description or "",
                "vote_count": votes,
                "ideas": [
                    {
                        "idea_id": idea.id,
                        "text": idea.corrected_text or idea.content or "",
                    }
                    for idea in ideas
                ],
            }
        )
    return out


def _chat(ws_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    q = (
        ChatMessage.query
        .filter_by(workshop_id=ws_id)
        .order_by(ChatMessage.timestamp.desc())
        .limit(limit)
    )
    rows = list(reversed(q.all()))
    return [
        {
            "ts": r.timestamp.isoformat() if r.timestamp else None,
            "user_id": r.user_id,
            "username": r.username,
            "message": r.message,
            "type": getattr(r, "message_type", "user"),
        }
        for r in rows
    ]


def _transcripts(ws_id: int, limit: int = 300) -> List[Dict[str, Any]]:
    q = (
        Transcript.query
        .filter_by(workshop_id=ws_id)
        .order_by(Transcript.created_timestamp.desc())
        .limit(limit)
    )
    rows = list(reversed(q.all()))
    return [
        {
            "ts": r.created_timestamp.isoformat() if r.created_timestamp else None,
            "user_id": r.user_id,
            "entry_type": getattr(r, "entry_type", "human"),
            "text": r.processed_transcript or r.raw_stt_transcript or "",
        }
        for r in rows
    ]


def _existing_notes(ws_id: int, limit: int = 80) -> List[Dict[str, Any]]:
    q = (
        DiscussionNote.query
        .filter_by(workshop_id=ws_id)
        .order_by(DiscussionNote.ts.desc())
        .limit(limit)
    )
    rows = list(reversed(q.all()))
    return [
        {
            "ts": note.ts.isoformat() if note.ts else None,
            "speaker_user_id": note.speaker_user_id,
            "point": note.point,
            "origin": note.origin,
        }
        for note in rows
    ]


def _existing_decisions(ws_id: int) -> List[Dict[str, Any]]:
    rows = (
        CapturedDecision.query
        .filter_by(workshop_id=ws_id)
        .order_by(CapturedDecision.created_at.asc())
        .all()
    )
    return [
        {
            "id": dec.id,
            "cluster_id": dec.cluster_id,
            "topic": dec.topic,
            "decision": dec.decision,
            "rationale": dec.rationale,
            "owner_user_id": dec.owner_user_id,
            "status": getattr(dec, "status", "draft"),
        }
        for dec in rows
    ]


def _forum_snapshot(ws_id: int) -> Dict[str, Any]:
    categories = ForumCategory.query.filter_by(workshop_id=ws_id).all()
    data: List[Dict[str, Any]] = []
    for cat in categories:
        topic_count = (
            ForumTopic.query
            .filter_by(workshop_id=ws_id, category_id=cat.id)
            .count()
        )
        data.append(
            {
                "id": cat.id,
                "title": cat.title,
                "description": cat.description,
                "topic_count": topic_count,
            }
        )
    return {"categories": data}


def _forum_details(ws_id: int, *, topic_limit: int = 5, post_limit: int = 5) -> Dict[str, Any]:
    categories = ForumCategory.query.filter_by(workshop_id=ws_id).all()
    output: List[Dict[str, Any]] = []
    for category in categories:
        topics = (
            ForumTopic.query
            .filter_by(workshop_id=ws_id, category_id=category.id)
            .order_by(ForumTopic.updated_at.desc())
            .limit(topic_limit)
            .all()
        )
        topic_payload: List[Dict[str, Any]] = []
        for topic in topics:
            posts = (
                ForumPost.query
                .filter_by(workshop_id=ws_id, topic_id=topic.id)
                .order_by(ForumPost.created_at.desc())
                .limit(post_limit)
                .all()
            )
            topic_payload.append(
                {
                    "id": topic.id,
                    "title": topic.title,
                    "description": topic.description,
                    "pinned": getattr(topic, "pinned", False),
                    "locked": getattr(topic, "locked", False),
                    "updated_at": topic.updated_at.isoformat() if topic.updated_at else None,
                    "latest_posts": [
                        {
                            "id": post.id,
                            "user_id": post.user_id,
                            "preview": (post.body or "")[:280].strip(),
                            "created_at": post.created_at.isoformat() if post.created_at else None,
                            "reply_count": ForumReply.query.filter_by(post_id=post.id).count(),
                        }
                        for post in posts
                    ],
                    "ai_assist_types": [assist.type for assist in topic.ai_assists.limit(8).all()],
                }
            )
        output.append(
            {
                "id": category.id,
                "title": category.title,
                "description": category.description,
                "cluster_id": category.cluster_id,
                "topics": topic_payload,
            }
        )
    return {"categories": output}


def _settings_to_payload(settings: DiscussionSettings | None) -> Dict[str, Any]:
    if not settings:
        return {
            "mediator_interval_secs": DEFAULT_MEDIATOR_INTERVAL,
            "scribe_interval_secs": DEFAULT_SCRIBE_INTERVAL,
            "last_mediator_run_at": None,
            "last_scribe_run_at": None,
            "auto_seed_forum": True,
        }
    return {
        "mediator_interval_secs": settings.mediator_interval_secs,
        "scribe_interval_secs": settings.scribe_interval_secs,
        "last_mediator_run_at": settings.last_mediator_run_at.isoformat() if settings.last_mediator_run_at else None,
        "last_scribe_run_at": settings.last_scribe_run_at.isoformat() if settings.last_scribe_run_at else None,
        "auto_seed_forum": settings.auto_seed_forum,
    }


def _extract_feasibility_annex(feasibility: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(feasibility, dict):
        return {}
    analysis = feasibility.get("analysis") or {}
    document_spec = feasibility.get("document_spec") or {}
    annex: Dict[str, Any] = {}
    for key in ("risk_annex", "risk_register", "risks"):
        value = feasibility.get(key)
        if value:
            annex["risk_register"] = value
            break
    if "risk_register" not in annex:
        candidate = analysis.get("risk_register") or analysis.get("risks")
        if candidate:
            annex["risk_register"] = candidate
    method_notes = analysis.get("method_notes")
    if method_notes:
        annex["method_notes"] = method_notes
    clusters = analysis.get("clusters")
    if clusters:
        annex["cluster_findings"] = clusters
    appendices = document_spec.get("appendices")
    if appendices:
        annex["appendices"] = appendices
    return annex


def _extract_framing_risk_checklist(framing: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(framing, dict):
        return {}
    for key in ("risk_checklist", "risk_register", "risks"):
        value = framing.get(key)
        if value:
            return {"items": value, "source": key}
    return {}


def _action_items_payload(ws_id: int) -> List[Dict[str, Any]]:
    items = (
        ActionItem.query
        .filter_by(workshop_id=ws_id)
        .order_by(ActionItem.created_at.asc())
        .all()
    )
    payload: List[Dict[str, Any]] = []
    for item in items:
        payload.append(
            {
                "id": item.id,
                "title": item.title,
                "status": item.status,
                "owner_participant_id": item.owner_participant_id,
                "due_date": item.due_date.isoformat() if getattr(item, "due_date", None) else None,
                "task_id": item.task_id,
            }
        )
    return payload


def _ensure_settings(workshop_id: int) -> DiscussionSettings:
    settings = DiscussionSettings.query.filter_by(workshop_id=workshop_id).first()
    if settings:
        return settings
    settings = DiscussionSettings()
    settings.workshop_id = workshop_id
    settings.mediator_interval_secs = DEFAULT_MEDIATOR_INTERVAL
    settings.scribe_interval_secs = DEFAULT_SCRIBE_INTERVAL
    settings.auto_seed_forum = True
    db.session.add(settings)
    db.session.flush()
    return settings


def _collect_discussion_state(workshop_id: int, phase_context: str | None = None) -> Tuple[Workshop, DiscussionSettings, Dict[str, Any]]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        raise DiscussionStateError("Workshop not found", status_code=404)

    settings = _ensure_settings(workshop_id)
    overview = {
        "title": ws.title,
        "objective": ws.objective or "TBD",
        "scheduled_for": ws.date_time.strftime("%Y-%m-%d %H:%M UTC") if ws.date_time else "unscheduled",
    }
    framing = _latest_payload(workshop_id, ["framing"]) or {}
    feasibility = _latest_payload(workshop_id, ["results_feasibility"]) or {}
    prioritization = _latest_payload(workshop_id, ["results_prioritization"]) or {}
    state = {
        "overview": overview,
        "framing": framing,
        "feasibility": feasibility,
        "prioritization": prioritization,
        "clusters": _clusters(workshop_id),
        "chat": _chat(workshop_id),
        "transcripts": _transcripts(workshop_id),
        "notes": _existing_notes(workshop_id),
        "decisions": _existing_decisions(workshop_id),
        "forum_snapshot": _forum_snapshot(workshop_id),
        "forum_detailed": _forum_details(workshop_id),
        "feasibility_annex": _extract_feasibility_annex(feasibility),
        "framing_risk_checklist": _extract_framing_risk_checklist(framing),
        "action_items": _action_items_payload(workshop_id),
        "settings": settings,
        "phase_context": phase_context or "Facilitated discussion focusing on top decisions.",
    }
    return ws, settings, state


def _default_mode_payload(mode: Mode, state: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "discussion_notes": state.get("notes") or [],
        "decisions": state.get("decisions") or [],
    }
    if mode == "initial":
        payload["task_duration"] = DISCUSSION_TASK_DURATION
    elif mode == "devil_advocate":
        clusters = state.get("clusters") or []
        focus = sorted(clusters, key=lambda c: c.get("vote_count", 0), reverse=True)
        payload["focus_clusters"] = focus[:5]
    elif mode == "mediator":
        notes = state.get("notes") or []
        payload["recent_notes"] = notes[-12:]
    elif mode == "scribe":
        payload["recent_chat"] = (state.get("chat") or [])[-15:]
        payload["recent_transcripts"] = (state.get("transcripts") or [])[-20:]
    return payload


def _determine_mode(state: Dict[str, Any], fallback: Mode = "initial") -> Mode:
    if (state.get("notes") or state.get("decisions")):
        return "devil_advocate"
    return fallback


def _build_discussion_task_payload(data: Dict[str, Any], state: Dict[str, Any], mode: Mode) -> Dict[str, Any]:
    return {
        "title": "Open Discussion",
        "task_type": "discussion",
        "task_description": "Let’s focus debate on the top decisions. Use chat and forum to contribute.",
        "instructions": "Share your reasoning, ask clarifying questions, and react to the devil’s-advocate prompts. The mediator will propose a decision every few minutes.",
        "task_duration": DISCUSSION_TASK_DURATION,
        "narration": data.get("narration") or "",
        "tts_script": data.get("tts_script") or "",
        "tts_read_time_seconds": int(data.get("tts_read_time_seconds") or 60),
        "discussion_notes": data.get("discussion_notes") or [],
        "decisions": data.get("decisions") or [],
        "devil_advocate": data.get("devil_advocate") or [],
        "mediator_prompt": data.get("mediator_prompt") or "",
        "scribe_summary": data.get("scribe_summary") or "",
        "forum_snapshot": state.get("forum_snapshot"),
        "forum_detailed": state.get("forum_detailed"),
        "feasibility_annex": state.get("feasibility_annex"),
        "framing_risk_checklist": state.get("framing_risk_checklist"),
        "action_items": state.get("action_items"),
        "phase_context": state.get("phase_context"),
        "mode": mode,
    }


def _compose_prompt_inputs(state: Dict[str, Any], mode: Mode, *, mode_payload_override: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, str], Dict[str, Any]]:
    payload = _default_mode_payload(mode, state)
    if mode_payload_override:
        payload.update(mode_payload_override)
    settings_payload = _settings_to_payload(state.get("settings"))
    contract = get_mode_contract(mode)
    inputs = {
        "mode": mode,
        "mode_instruction": contract.instruction,
        "schema_block": contract.schema_directive,
        "workshop_overview": _safe_json(state.get("overview")),
        "framing_core": _safe_json({
            "problem_statement": (state.get("framing") or {}).get("problem_statement"),
            "success_criteria": (state.get("framing") or {}).get("success_criteria"),
            "constraints": (state.get("framing") or {}).get("constraints"),
        }),
        "prioritized_json": _safe_json({
            "prioritized": (state.get("prioritization") or {}).get("prioritized"),
            "open_unknowns": (state.get("prioritization") or {}).get("open_unknowns"),
            "notable_findings": (state.get("prioritization") or {}).get("notable_findings"),
            "risks": (state.get("prioritization") or {}).get("risks"),
        }),
        "feasibility_json": _safe_json({
            "analysis": (state.get("feasibility") or {}).get("analysis"),
            "document": (state.get("feasibility") or {}).get("document_spec"),
        }),
        "clusters_json": _safe_json(state.get("clusters")),
        "chat_json": _safe_json(state.get("chat")),
        "transcripts_json": _safe_json(state.get("transcripts")),
        "prior_notes_json": _safe_json(state.get("notes")),
        "prior_decisions_json": _safe_json(state.get("decisions")),
        "forum_snapshot_json": _safe_json(state.get("forum_snapshot")),
        "forum_detailed_json": _safe_json(state.get("forum_detailed")),
        "feasibility_annex_json": _safe_json(state.get("feasibility_annex")),
        "framing_risk_checklist_json": _safe_json(state.get("framing_risk_checklist")),
        "action_items_json": _safe_json(state.get("action_items")),
        "cadence_settings_json": _safe_json(settings_payload),
        "mode_payload_json": _safe_json(payload),
        "phase_context": state.get("phase_context") or "",
    }
    return inputs, payload


def _hash_inputs(inputs: Dict[str, str]) -> str:
    try:
        serialized = json.dumps(inputs, sort_keys=True)
    except TypeError:
        serialized = json.dumps({k: str(v) for k, v in inputs.items()}, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _invoke_discussion_llm(inputs: Dict[str, str]) -> Tuple[Dict[str, Any], int, str, str]:
    llm = get_chat_llm(model_kwargs={"temperature": 0.35, "max_tokens": 1800})
    prompt = build_prompt_template()
    start = time.perf_counter()
    raw = (prompt | llm).invoke(inputs)
    latency_ms = int((time.perf_counter() - start) * 1000)

    def _to_text(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, str):
            return val
        if hasattr(val, "content"):
            content = getattr(val, "content")
            if isinstance(content, list):
                return "".join(str(part) for part in content)
            return str(content)
        if isinstance(val, list):
            return "".join(str(part) for part in val)
        if isinstance(val, dict):
            return json.dumps(val, ensure_ascii=False)
        return str(val)

    text_response = _to_text(raw)
    block_raw = extract_json_block(text_response)
    candidate = block_raw or text_response
    if not isinstance(candidate, str):
        candidate = json.dumps(candidate, ensure_ascii=False)
    try:
        data = json.loads(candidate)
        if not isinstance(data, dict):
            raise ValueError("Model response was not a JSON object")
    except Exception as exc:
        raise DiscussionStateError(f"Discussion generation error: {exc}", status_code=503)

    model_used = getattr(llm, "model_id", Config.BEDROCK_MODEL_ID)
    return data, latency_ms, model_used, text_response


def _execute_discussion_mode(
    workshop_id: int,
    *,
    requested_mode: Mode | None,
    actor_user_id: Optional[int],
    phase_context: str | None,
    overrides: Optional[Dict[str, Any]],
    allow_forum_seed: bool,
    enforce_cadence: bool,
    create_task: bool,
) -> Dict[str, Any]:
    lock = _lock_for(workshop_id)
    with lock:
        try:
            ws, settings, state = _collect_discussion_state(workshop_id, phase_context)
        except DiscussionStateError:
            raise

        mode = requested_mode or _determine_mode(state)
        if enforce_cadence:
            _enforce_mode_cadence(mode, settings)

        inputs, _ = _compose_prompt_inputs(state, mode, mode_payload_override=overrides)
        checksum = _hash_inputs(inputs)

        try:
            data, latency_ms, model_id, _ = _invoke_discussion_llm(inputs)
        except DiscussionStateError as exc:
            _log_discussion_run(
                workshop_id,
                mode,
                model_id=Config.BEDROCK_MODEL_ID,
                latency_ms=0,
                checksum=checksum,
                response_json=None,
                error=str(exc),
                actor_user_id=actor_user_id,
            )
            db.session.rollback()
            raise
        except Exception:
            db.session.rollback()
            raise

        run = _log_discussion_run(
            workshop_id,
            mode,
            model_id=model_id,
            latency_ms=latency_ms,
            checksum=checksum,
            response_json=data,
            error=None,
            actor_user_id=actor_user_id,
        )

        outputs = _apply_mode_outputs(workshop_id, settings, mode, data, allow_forum_seed=allow_forum_seed)
        settings_payload = _settings_to_payload(settings)

        task_payload: Optional[Dict[str, Any]] = None
        task_id: Optional[int] = None
        if create_task:
            task_payload = _build_discussion_task_payload(data, state, mode)
            task = BrainstormTask()
            task.workshop_id = workshop_id
            task.task_type = "discussion"
            task.title = task_payload["title"]
            task.description = task_payload["task_description"]
            task.duration = task_payload["task_duration"]
            task.status = "pending"
            serialized = json.dumps(task_payload, ensure_ascii=False)
            task.prompt = serialized
            task.payload_json = serialized
            db.session.add(task)
            db.session.flush()
            task_id = task.id
            task_payload["task_id"] = task_id

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("[Discussion] Failed to execute mode %s", mode)
            raise

    _broadcast_mode_results(workshop_id, outputs)
    if create_task and task_payload:
        _emit_discussion_event(workshop_id, "discussion_payload_updated", {"payload": task_payload})

    return {
        "mode": mode,
        "data": data,
        "outputs": outputs,
        "settings": settings_payload,
        "run_id": run.id,
        "latency_ms": latency_ms,
        "model_id": model_id,
        "task_payload": task_payload,
        "task_id": task_id,
    }


def _log_discussion_run(
    workshop_id: int,
    mode: Mode,
    *,
    model_id: str,
    latency_ms: int,
    checksum: str,
    response_json: Dict[str, Any] | None,
    error: str | None,
    actor_user_id: Optional[int],
) -> DiscussionRun:
    run = DiscussionRun()
    run.workshop_id = workshop_id
    run.mode = mode
    run.llm_model = model_id
    run.latency_ms = latency_ms
    run.input_checksum = checksum
    if response_json is not None:
        try:
            run.response_json = json.dumps(response_json, ensure_ascii=False)
        except TypeError:
            run.response_json = json.dumps(response_json, default=str, ensure_ascii=False)
    if error:
        run.error = error
    if actor_user_id:
        run.created_by_id = actor_user_id
    db.session.add(run)
    db.session.flush()
    return run


def _persist_discussion_notes(workshop_id: int, notes: List[Dict[str, Any]], origin: str) -> List[Dict[str, Any]]:
    created: List[DiscussionNote] = []
    for payload in notes:
        point = (payload.get("point") or "").strip()
        if not point:
            continue
        existing = (
            DiscussionNote.query
            .filter(
                DiscussionNote.workshop_id == workshop_id,
                DiscussionNote.origin == origin,
                func.lower(DiscussionNote.point) == point.lower(),
            )
            .first()
        )
        if existing:
            continue
        note = DiscussionNote()
        note.workshop_id = workshop_id
        note.point = point
        note.origin = origin
        speaker_id = payload.get("speaker_user_id")
        if isinstance(speaker_id, int):
            note.speaker_user_id = speaker_id
        ts_raw = payload.get("ts")
        if ts_raw:
            try:
                note.ts = datetime.fromisoformat(ts_raw)
            except Exception:
                note.ts = datetime.utcnow()
        else:
            note.ts = datetime.utcnow()
        db.session.add(note)
        created.append(note)
    if created:
        db.session.flush()
    return [
        {
            "id": note.id,
            "ts": note.ts.isoformat() if note.ts else None,
            "speaker_user_id": note.speaker_user_id,
            "point": note.point,
            "origin": note.origin,
        }
        for note in created
    ]


def _persist_decisions(workshop_id: int, decisions: List[Dict[str, Any]], status: str) -> List[Dict[str, Any]]:
    updated: List[CapturedDecision] = []
    for payload in decisions:
        topic = (payload.get("topic") or "").strip()
        content = (payload.get("decision") or "").strip()
        if not topic and not content:
            continue
        key_topic = topic.lower()
        key_decision = content.lower()
        existing = (
            CapturedDecision.query
            .filter(
                CapturedDecision.workshop_id == workshop_id,
                func.lower(CapturedDecision.topic) == key_topic,
                func.lower(CapturedDecision.decision) == key_decision,
            )
            .first()
        )
        decision_obj = existing or CapturedDecision()
        decision_obj.workshop_id = workshop_id
        decision_obj.topic = topic or (content[:120] if content else "Decision")
        decision_obj.decision = content or topic
        decision_obj.rationale = payload.get("rationale") or payload.get("rational")
        owner = payload.get("owner_user_id")
        if isinstance(owner, int):
            decision_obj.owner_user_id = owner
        decision_obj.cluster_id = payload.get("cluster_id")
        decision_obj.status = status
        if status != "confirmed":
            decision_obj.confirmed_at = None
            decision_obj.confirmed_by_user_id = None
        if not existing:
            db.session.add(decision_obj)
        updated.append(decision_obj)
    if updated:
        db.session.flush()
    return [
        {
            "id": dec.id,
            "topic": dec.topic,
            "decision": dec.decision,
            "rationale": dec.rationale,
            "owner_user_id": dec.owner_user_id,
            "cluster_id": dec.cluster_id,
            "status": dec.status,
            "confirmed_at": dec.confirmed_at.isoformat() if dec.confirmed_at else None,
            "confirmed_by_user_id": dec.confirmed_by_user_id,
        }
        for dec in updated
    ]


def _find_forum_topic(workshop_id: int, cluster_id: Optional[int]) -> Optional[ForumTopic]:
    if cluster_id is None:
        return None
    category = ForumCategory.query.filter_by(workshop_id=workshop_id, cluster_id=cluster_id).first()
    if not category:
        return None
    topic = (
        ForumTopic.query
        .filter_by(workshop_id=workshop_id, category_id=category.id)
        .order_by(ForumTopic.id.asc())
        .first()
    )
    return topic


def _record_forum_assist(workshop_id: int, entries: List[Dict[str, Any]], assist_type: str) -> None:
    for entry in entries:
        topic = _find_forum_topic(workshop_id, entry.get("cluster_id"))
        if not topic:
            continue
        content = json.dumps(entry, ensure_ascii=False)
        assist = ForumAIAssist()
        assist.forum_topic_id = topic.id
        assist.type = assist_type
        assist.content = content
        db.session.add(assist)


def _emit_discussion_event(workshop_id: int, event: str, payload: Dict[str, Any]) -> None:
    try:
        socketio.emit(event, payload, to=f"workshop_{workshop_id}")
    except Exception:
        current_app.logger.warning("[Discussion] Failed to emit %s for workshop %s", event, workshop_id, exc_info=True)


def _seed_forum_if_needed(workshop_id: int, settings: DiscussionSettings) -> bool:
    if not settings.auto_seed_forum:
        return False
    try:
        seed_forum_from_results(workshop_id)
        settings.auto_seed_forum = False
        db.session.flush()
        return True
    except Exception:
        current_app.logger.warning("[Discussion] Forum seeding failed", exc_info=True)
        return False


def _enforce_mode_cadence(mode: Mode, settings: DiscussionSettings) -> None:
    now = datetime.utcnow()
    if mode == "mediator":
        last = settings.last_mediator_run_at or datetime.fromtimestamp(0)
        interval = settings.mediator_interval_secs or DEFAULT_MEDIATOR_INTERVAL
        remaining = interval - int((now - last).total_seconds())
        if remaining > 0:
            raise DiscussionStateError(f"Mediator can run again in {remaining} seconds", status_code=429)
        settings.last_mediator_run_at = now
    elif mode == "scribe":
        last = settings.last_scribe_run_at or datetime.fromtimestamp(0)
        interval = settings.scribe_interval_secs or DEFAULT_SCRIBE_INTERVAL
        remaining = interval - int((now - last).total_seconds())
        if remaining > 0:
            raise DiscussionStateError(f"Scribe can run again in {remaining} seconds", status_code=429)
        settings.last_scribe_run_at = now


def _apply_mode_outputs(
    workshop_id: int,
    settings: DiscussionSettings,
    mode: Mode,
    data: Dict[str, Any],
    *,
    allow_forum_seed: bool = False,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    if allow_forum_seed:
        seeded = _seed_forum_if_needed(workshop_id, settings)
        if seeded:
            results["forum_seeded"] = True

    origin = NOTE_ORIGIN_BY_MODE.get(mode)
    if origin and isinstance(data.get("discussion_notes"), list):
        created_notes = _persist_discussion_notes(workshop_id, data.get("discussion_notes") or [], origin)
        if created_notes:
            results["discussion_notes"] = created_notes

    if mode in DECISION_STATUS_BY_MODE and isinstance(data.get("decisions"), list):
        status = DECISION_STATUS_BY_MODE[mode]
        updated = _persist_decisions(workshop_id, data.get("decisions") or [], status)
        if updated:
            results["decisions"] = updated

    if mode == "devil_advocate" and isinstance(data.get("devil_advocate"), list):
        _record_forum_assist(workshop_id, data["devil_advocate"], "devil_advocate")
        results["devil_advocate"] = data["devil_advocate"]
    if mode == "scribe":
        summary = data.get("scribe_summary")
        if summary:
            _record_forum_assist(workshop_id, [{"cluster_id": entry.get("cluster_id"), "summary": summary} for entry in data.get("discussion_notes") or []], "scribe")
            results["scribe_summary"] = summary
    if mode == "mediator":
        mediator_prompt = data.get("mediator_prompt")
        if mediator_prompt:
            results["mediator_prompt"] = mediator_prompt

    return results


def _broadcast_mode_results(workshop_id: int, outputs: Dict[str, Any]) -> None:
    if outputs.get("discussion_notes"):
        _emit_discussion_event(workshop_id, "discussion_notes_added", {"notes": outputs["discussion_notes"]})
        _emit_discussion_event(workshop_id, "discussion_notes_updated", {"notes": outputs["discussion_notes"]})
    if outputs.get("decisions"):
        _emit_discussion_event(workshop_id, "discussion_decisions_updated", {"decisions": outputs["decisions"]})
        _emit_discussion_event(workshop_id, "decisions_updated", {"decisions": outputs["decisions"]})
    if outputs.get("devil_advocate"):
        _emit_discussion_event(workshop_id, "discussion_devil_advocate", {"items": outputs["devil_advocate"]})
        _emit_discussion_event(workshop_id, "devil_advocate_ready", {"items": outputs["devil_advocate"]})
    if outputs.get("scribe_summary"):
        _emit_discussion_event(workshop_id, "discussion_scribe_summary", {"summary": outputs["scribe_summary"]})
        _emit_discussion_event(workshop_id, "scribe_summary_ready", {"summary": outputs["scribe_summary"]})
    if outputs.get("mediator_prompt"):
        _emit_discussion_event(workshop_id, "mediator_prompt_ready", {"prompt": outputs["mediator_prompt"]})
    if outputs.get("forum_seeded"):
        _emit_discussion_event(workshop_id, "discussion_forum_seeded", {"success": True})
        _emit_discussion_event(workshop_id, "forum_seed_done", {"success": True})


def get_discussion_payload(workshop_id: int, phase_context: str | None = None) -> Dict[str, Any] | Tuple[str, int]:
    try:
        result = _execute_discussion_mode(
            workshop_id,
            requested_mode=None,
            actor_user_id=_acting_user_id(),
            phase_context=phase_context,
            overrides=None,
            allow_forum_seed=True,
            enforce_cadence=False,
            create_task=True,
        )
    except DiscussionStateError as exc:
        return str(exc), exc.status_code
    except Exception:
        current_app.logger.exception("[Discussion] Failed to generate discussion payload")
        return "Failed to persist discussion payload", 500

    payload = result.get("task_payload")
    if not payload:
        return "Failed to persist discussion payload", 500
    return payload


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion", methods=["GET"])
@login_required
def api_get_discussion(workshop_id: int):
    _user_id, error = _require_access(workshop_id, organizer=False)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    result = get_discussion_payload(workshop_id)
    if isinstance(result, tuple):
        message, status = result
        return jsonify({"error": message}), status
    return jsonify(result)


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/refresh", methods=["POST"])
@login_required
def api_refresh_discussion(workshop_id: int):
    user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    body = request.get_json(silent=True) or {}
    mode_raw = (body.get("mode") or "").strip().lower()
    try:
        mode = cast(Mode, mode_raw)
        get_mode_contract(mode)
    except KeyError:
        return jsonify({"error": "Invalid discussion mode."}), 400

    overrides = body.get("mode_payload")
    if overrides is not None and not isinstance(overrides, dict):
        return jsonify({"error": "mode_payload must be an object"}), 400
    phase_context = body.get("phase_context")
    try:
        result = _execute_discussion_mode(
            workshop_id,
            requested_mode=mode,
            actor_user_id=user_id,
            phase_context=phase_context,
            overrides=overrides,
            allow_forum_seed=(mode == "initial"),
            enforce_cadence=True,
            create_task=False,
        )
    except DiscussionStateError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    except Exception:
        current_app.logger.exception("[Discussion] Failed to refresh mode %s", mode)
        return jsonify({"error": "Failed to refresh discussion"}), 500

    return jsonify({
        "mode": result.get("mode", mode),
        "payload": result.get("data"),
        "outputs": result.get("outputs"),
        "settings": result.get("settings"),
        "run_id": result.get("run_id"),
        "latency_ms": result.get("latency_ms"),
        "model_id": result.get("model_id"),
    })


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/run", methods=["POST"])
@login_required
def api_run_discussion(workshop_id: int):
    user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    body = request.get_json(silent=True) or {}
    mode_raw = body.get("mode")
    requested_mode: Mode | None = None
    if mode_raw:
        try:
            requested_mode = cast(Mode, str(mode_raw).strip().lower())
            get_mode_contract(requested_mode)
        except KeyError:
            return jsonify({"error": "Invalid discussion mode."}), 400

    overrides = body.get("mode_payload")
    if overrides is not None and not isinstance(overrides, dict):
        return jsonify({"error": "mode_payload must be an object"}), 400
    phase_context = body.get("phase_context")

    try:
        result = _execute_discussion_mode(
            workshop_id,
            requested_mode=requested_mode,
            actor_user_id=user_id,
            phase_context=phase_context,
            overrides=overrides,
            allow_forum_seed=True,
            enforce_cadence=False,
            create_task=True,
        )
    except DiscussionStateError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    except Exception:
        current_app.logger.exception("[Discussion] Failed to run orchestrator")
        return jsonify({"error": "Failed to run discussion"}), 500

    payload = result.get("task_payload")
    if not payload:
        return jsonify({"error": "Failed to persist discussion payload"}), 500

    return jsonify({
        "mode": result.get("mode"),
        "task": payload,
        "outputs": result.get("outputs"),
        "settings": result.get("settings"),
        "run_id": result.get("run_id"),
        "latency_ms": result.get("latency_ms"),
        "model_id": result.get("model_id"),
    })


def _execute_special_mode_endpoint(
    workshop_id: int,
    *,
    user_id: int,
    mode: Mode,
    enforce_cadence: bool,
    allow_forum_seed: bool = False,
) -> Tuple[Dict[str, Any], int]:
    body = request.get_json(silent=True) or {}
    overrides = body.get("mode_payload")
    if overrides is not None and not isinstance(overrides, dict):
        return {"error": "mode_payload must be an object"}, 400
    phase_context = body.get("phase_context")

    try:
        result = _execute_discussion_mode(
            workshop_id,
            requested_mode=mode,
            actor_user_id=user_id,
            phase_context=phase_context,
            overrides=cast(Optional[Dict[str, Any]], overrides),
            allow_forum_seed=allow_forum_seed,
            enforce_cadence=enforce_cadence,
            create_task=False,
        )
    except DiscussionStateError as exc:
        return {"error": str(exc)}, exc.status_code
    except Exception:
        current_app.logger.exception("[Discussion] Failed to execute %s mode", mode)
        return {"error": f"Failed to execute {mode} mode"}, 500

    return {
        "mode": result.get("mode", mode),
        "payload": result.get("data"),
        "outputs": result.get("outputs"),
        "settings": result.get("settings"),
        "run_id": result.get("run_id"),
        "latency_ms": result.get("latency_ms"),
        "model_id": result.get("model_id"),
    }, 200


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/devil-advocate", methods=["POST"])
@login_required
def api_discussion_devil_advocate(workshop_id: int):
    user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    payload, status = _execute_special_mode_endpoint(
        workshop_id,
        user_id=user_id,
        mode="devil_advocate",
        enforce_cadence=False,
    )
    if status != 200:
        return jsonify(payload), status
    return jsonify(payload)


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/mediator", methods=["POST"])
@login_required
def api_discussion_mediator(workshop_id: int):
    user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    payload, status = _execute_special_mode_endpoint(
        workshop_id,
        user_id=user_id,
        mode="mediator",
        enforce_cadence=True,
    )
    if status != 200:
        return jsonify(payload), status
    return jsonify(payload)


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/scribe", methods=["POST"])
@login_required
def api_discussion_scribe(workshop_id: int):
    user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    payload, status = _execute_special_mode_endpoint(
        workshop_id,
        user_id=user_id,
        mode="scribe",
        enforce_cadence=True,
    )
    if status != 200:
        return jsonify(payload), status
    return jsonify(payload)


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/forum-seed", methods=["POST"])
@login_required
def api_discussion_forum_seed(workshop_id: int):
    user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    lock = _lock_for(workshop_id)
    with lock:
        try:
            settings = _ensure_settings(workshop_id)
            seed_forum_from_results(workshop_id)
            settings.auto_seed_forum = False
            db.session.flush()
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("[Discussion] Failed to seed forum manually")
            return jsonify({"error": "Failed to seed forum"}), 500

    _emit_discussion_event(workshop_id, "discussion_forum_seeded", {"success": True, "actor_user_id": user_id})
    _emit_discussion_event(workshop_id, "forum_seed_done", {"success": True, "actor_user_id": user_id})
    return jsonify({"success": True})


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/decision/<int:decision_id>/confirm", methods=["POST"])
@login_required
def api_confirm_decision(workshop_id: int, decision_id: int):
    user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    lock = _lock_for(workshop_id)
    with lock:
        decision = db.session.get(CapturedDecision, decision_id)
        if not decision or decision.workshop_id != workshop_id:
            return jsonify({"error": "Decision not found."}), 404
        decision.status = "confirmed"
        decision.confirmed_at = datetime.utcnow()
        decision.confirmed_by_user_id = user_id
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("[Discussion] Failed to confirm decision %s", decision_id)
            return jsonify({"error": "Failed to confirm decision"}), 500

    payload = {
        "id": decision.id,
        "topic": decision.topic,
        "decision": decision.decision,
        "rationale": decision.rationale,
        "owner_user_id": decision.owner_user_id,
        "cluster_id": decision.cluster_id,
        "status": decision.status,
        "confirmed_at": decision.confirmed_at.isoformat() if decision.confirmed_at else None,
        "confirmed_by_user_id": decision.confirmed_by_user_id,
    }
    _emit_discussion_event(workshop_id, "discussion_decision_confirmed", {"decision": payload})
    return jsonify(payload)


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/settings", methods=["POST"])
@login_required
def api_update_discussion_settings(workshop_id: int):
    _user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    body = request.get_json(silent=True) or {}
    mediator_interval = body.get("mediator_interval_secs")
    if mediator_interval is not None:
        if not isinstance(mediator_interval, int) or mediator_interval <= 0:
            return jsonify({"error": "mediator_interval_secs must be a positive integer"}), 400
    scribe_interval = body.get("scribe_interval_secs")
    if scribe_interval is not None:
        if not isinstance(scribe_interval, int) or scribe_interval <= 0:
            return jsonify({"error": "scribe_interval_secs must be a positive integer"}), 400
    auto_seed = body.get("auto_seed_forum") if "auto_seed_forum" in body else None
    seed_now = bool(body.get("seed_forum_now"))

    lock = _lock_for(workshop_id)
    with lock:
        try:
            settings = _ensure_settings(workshop_id)
            if mediator_interval is not None:
                settings.mediator_interval_secs = mediator_interval
            if scribe_interval is not None:
                settings.scribe_interval_secs = scribe_interval
            if auto_seed is not None:
                settings.auto_seed_forum = bool(auto_seed)
            db.session.flush()
            if seed_now:
                _seed_forum_if_needed(workshop_id, settings)
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("[Discussion] Failed to update settings")
            return jsonify({"error": "Failed to update settings"}), 500

    payload = _settings_to_payload(settings)
    _emit_discussion_event(workshop_id, "discussion_settings_updated", {"settings": payload})
    return jsonify(payload)


@discussion_bp.route("/api/workshops/<int:workshop_id>/discussion/history", methods=["GET"])
@login_required
def api_discussion_history(workshop_id: int):
    _user_id, error = _require_access(workshop_id, organizer=True)
    if error:
        message, status = error
        return jsonify({"error": message}), status

    runs = (
        DiscussionRun.query
        .filter_by(workshop_id=workshop_id)
        .order_by(DiscussionRun.created_at.desc())
        .limit(40)
        .all()
    )
    payload = [
        {
            "id": run.id,
            "mode": run.mode,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "latency_ms": run.latency_ms,
            "model": run.llm_model,
            "error": bool(run.error),
        }
        for run in runs
    ]
    return jsonify({"history": payload})
