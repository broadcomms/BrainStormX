# Core Socket.IO hub for BrainStormX workshops.
# (Content migrated from former sockets.py, unchanged logic.)
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple, Any, DefaultDict, Optional

from flask import current_app, request, has_request_context
from flask_socketio import emit, join_room, leave_room
from sqlalchemy import func
from sqlalchemy.orm import selectinload  # type: ignore

from app.config import TASK_SEQUENCE  # type: ignore
from app.assistant.assistant_socket import emit_assistant_state
from app.extensions import socketio, db  # type: ignore
from app.models import (
    User,
    Workshop,
    WorkshopParticipant,
    ChatMessage,
    Transcript,
    BrainstormTask,
    BrainstormIdea,
    IdeaCluster,
    IdeaVote,
    GenericVote,
    WorkshopPlanItem,
)  # type: ignore

from app.service.routes.moderator import (
    initialize_participant_tracking,  # type: ignore
    cleanup_participant_tracking,  # type: ignore
)

_sid_registry: Dict[str, Dict] = {}
_room_presence: Dict[str, set] = defaultdict(set)
_timer_thread: threading.Thread | None = None
_timer_thread_stop = threading.Event()
# Last-known presentation viewer state per (workshop_id, task_id)
_presentation_state: Dict[Tuple[int, int], dict] = {}
# Last-known feasibility viewer state per (workshop_id, task_id)
_feasibility_state: Dict[Tuple[int, int], dict] = {}
# Last-known prioritization viewer state per (workshop_id, task_id)
_prioritization_state: Dict[Tuple[int, int], dict] = {}
# Last-known action plan viewer state per (workshop_id, task_id)
_action_plan_state: Dict[Tuple[int, int], dict] = {}
# Last-known lightweight UI flags per workshop (e.g., 'showRationaleAll')
_ui_flags: DefaultDict[int, Dict[str, Any]] = defaultdict(dict)


def _timer_loop():
    while not _timer_thread_stop.is_set():
        try:
            time.sleep(1)
            active_workshops = Workshop.query.filter(
                Workshop.status.in_(["inprogress", "paused"])
            ).all()
            for ws in active_workshops:
                room = f"workshop_room_{ws.id}"
                if not ws.current_task_id:
                    continue
                remaining = ws.get_remaining_task_time()
                emit_timer_sync(
                    room,
                    {
                        "task_id": ws.current_task_id,
                        "remaining_seconds": remaining,
                        "is_paused": ws.status == "paused",
                    },
                    workshop_id=ws.id,
                )
                if ws.status == "inprogress" and remaining <= 0:
                    task = ws.current_task
                    if not task or task.status != "running":
                        continue
                    task.status = "completed"
                    task.ended_at = datetime.utcnow()
                    db.session.commit()
                    current_app.logger.info(
                        f"Auto-completed task {task.id} for workshop {ws.id} due to time expiry"
                    )
                    socketio.emit(
                        "task_completed",
                        {"task_id": task.id, "workshop_id": ws.id},
                        to=room,
                    )
                    if getattr(ws, "auto_advance_enabled", True) and (
                        getattr(ws, "auto_advance_after_seconds", 0) or 0
                    ) >= 0:
                        delay = int(
                            getattr(ws, "auto_advance_after_seconds", 0) or 0
                        )
                        if delay > 0:
                            time.sleep(min(delay, 5))
                        try:
                            from app.workshop.advance import (
                                advance_to_next_task,  # type: ignore
                            )

                            ok, err = advance_to_next_task(ws.id)
                            if not ok:
                                if isinstance(err, str) and "No more tasks" in err:
                                    try:
                                        ws.status = "completed"
                                        ws.current_task_id = None
                                        ws.timer_start_time = None
                                        ws.timer_paused_at = None
                                        ws.timer_elapsed_before_pause = 0
                                        db.session.commit()
                                        socketio.emit(
                                            "workshop_stopped",
                                            {"workshop_id": ws.id},
                                            to=room,
                                        )
                                        current_app.logger.info(
                                            f"Workshop {ws.id} completed at end of sequence (auto-advance)"
                                        )
                                    except Exception as ce:  # noqa
                                        db.session.rollback()
                                        current_app.logger.error(
                                            f"Error completing workshop {ws.id} at end of sequence: {ce}"
                                        )
                                else:
                                    ws.current_task_id = None
                                    ws.timer_start_time = None
                                    ws.timer_paused_at = None
                                    ws.timer_elapsed_before_pause = 0
                                    db.session.commit()
                        except Exception as e:  # noqa
                            current_app.logger.error(
                                f"Auto-advance failure for workshop {ws.id}: {e}"
                            )
                    else:
                        ws.current_task_id = None
                        ws.timer_start_time = None
                        ws.timer_paused_at = None
                        ws.timer_elapsed_before_pause = 0
                        db.session.commit()
        except Exception as e:  # noqa
            current_app.logger.error(f"Timer loop error: {e}", exc_info=True)


def start_timer_thread():
    global _timer_thread
    if _timer_thread and _timer_thread.is_alive():
        return
    _timer_thread_stop.clear()
    _timer_thread = threading.Thread(
        target=_timer_loop, name="workshop-timer", daemon=True
    )
    _timer_thread.start()


def stop_timer_thread():
    if _timer_thread:
        _timer_thread_stop.set()


def _get_participant_payload(workshop_id: int) -> List[dict]:
    workshop = db.session.get(Workshop, workshop_id)
    if not workshop:
        return []

    facilitator_roles = {"organizer", "facilitator", "admin"}

    def _normalize_id(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    target_workshop_id = _normalize_id(workshop_id)
    if target_workshop_id is None:
        return []

    online_ids: set[int] = set()
    for info in _sid_registry.values():
        if _normalize_id(info.get("workshop_id")) != target_workshop_id:
            continue
        normalized_user_id = _normalize_id(info.get("user_id"))
        if normalized_user_id is not None:
            online_ids.add(normalized_user_id)

    if not online_ids:
        return []

    participants = (
        db.session.query(WorkshopParticipant)
        .options(selectinload(WorkshopParticipant.user))  # type: ignore[arg-type]
        .filter(WorkshopParticipant.workshop_id == target_workshop_id)
        .filter(WorkshopParticipant.user_id.in_(online_ids))
        .all()
    )
    participants_by_user_id = {p.user_id: p for p in participants}

    users = (
        db.session.query(User)
        .filter(User.user_id.in_(online_ids))
        .all()
    )
    users_by_id = {u.user_id: u for u in users}

    payload: List[dict] = []
    for user_id in sorted(online_ids):
        user = users_by_id.get(user_id)
        if not user:
            continue

        participant = participants_by_user_id.get(user_id)
        raw_role: Optional[str] = None
        if participant is not None:
            raw_role = getattr(participant, "role", None)
        if not raw_role:
            if user_id == workshop.created_by_id:
                raw_role = "organizer"
            elif participant is not None:
                raw_role = "participant"
            else:
                raw_role = "guest"

        role = raw_role.strip().lower() if isinstance(raw_role, str) else "participant"
        is_organizer = user_id == workshop.created_by_id or role == "organizer"
        is_facilitator = (
            role in facilitator_roles
            or bool(getattr(participant, "is_facilitator", False))
        )

        payload.append(
            {
                "user_id": user_id,
                "participant_id": getattr(participant, "id", None),
                "role": role,
                "is_organizer": is_organizer,
                "is_facilitator": is_facilitator,
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "display_name": getattr(user, "display_name", None),
                "profile_pic_url": getattr(user, "profile_pic_url", None),
                "email": user.email,
                "is_public_profile": bool(getattr(user, "is_public_profile", True)),
            }
        )

    return payload


def _broadcast_participant_list(room: str, workshop_id: int):
    emit(
        "participant_list_update",
        {"workshop_id": workshop_id, "participants": _get_participant_payload(workshop_id)},
        to=room,
    )


@socketio.on("connect")
def _on_connect():  # type: ignore
    sid = getattr(request, "sid", None) if has_request_context() else None
    current_app.logger.debug("Client %s connected", sid)


@socketio.on("disconnect")
def _on_disconnect():  # type: ignore
    sid = getattr(request, "sid", None) if has_request_context() else None
    if not isinstance(sid, str):
        current_app.logger.warning("Disconnect with invalid SID: %s", sid)
        return
    info = _sid_registry.pop(sid, None)
    if info:
        room, workshop_id, user_id = (
            info["room"],
            info["workshop_id"],
            info["user_id"],
        )
        if room in _room_presence:
            _room_presence[room].discard(user_id)
            if workshop_id and user_id:
                cleanup_participant_tracking(workshop_id, user_id)
            current_app.logger.debug(
                f"Client {sid} disconnected from {room} (user {user_id})"
            )
            if _room_presence[room]:
                _broadcast_participant_list(room, workshop_id)
            else:
                del _room_presence[room]
                current_app.logger.debug(f"Cleaned up empty room: {room}")
    else:
        # This can happen if the client already emitted leave_room, we replaced an older SID,
        # or cleanup happened earlier in another handler. Treat as benign.
        current_app.logger.debug(
            f"SID {sid} not found in presence tracking during disconnect (likely already cleaned up)."
        )


@socketio.on("join_room")
def _on_join_room(data):  # type: ignore
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    sid = getattr(request, "sid", None) if has_request_context() else None
    if not isinstance(sid, str):
        current_app.logger.warning("join_room with invalid SID: %s", sid)
        return
    if not all([room, workshop_id, user_id]):
        current_app.logger.warning(f"join_room incomplete data from {sid}: {data}")
        return
    # If this SID is already registered in the same room/workshop/user, avoid re-sending full state.
    # Treat re-emits as a lightweight scope/history refresh only to prevent client/server loops.
    existing_entry = _sid_registry.get(sid)
    if (
        existing_entry
        and existing_entry.get("room") == room
        and existing_entry.get("workshop_id") == workshop_id
        and existing_entry.get("user_id") == user_id
    ):
        try:
            scope = data.get("scope") or "workshop_chat"
            q = ChatMessage.query.filter_by(workshop_id=workshop_id)
            try:
                if scope in ("workshop_chat", "discussion_chat"):
                    q = q.filter(ChatMessage.chat_scope == scope)  # type: ignore[attr-defined]
            except Exception:
                pass
            chat_history = (
                q.order_by(ChatMessage.timestamp.desc()).limit(50).all()
            )
            chat_history.reverse()
            history_payload = []
            for msg in chat_history:
                try:
                    mtype = getattr(msg, "message_type", "user")
                except Exception:
                    mtype = "user"
                try:
                    cscope = getattr(msg, "chat_scope", "workshop_chat")
                except Exception:
                    cscope = "workshop_chat"
                history_payload.append(
                    {
                        "user_name": msg.username,
                        "message": msg.message,
                        "timestamp": msg.timestamp.isoformat(),
                        "message_type": mtype,
                        "chat_scope": cscope,
                    }
                )
            emit("chat_history", {"messages": history_payload}, to=sid)
            current_app.logger.debug(
                f"Re-join (scope refresh) for SID {sid} in {room}; emitted chat_history only."
            )
        except Exception as e:  # noqa
            current_app.logger.warning(
                f"Failed lightweight chat history emit for workshop {workshop_id}, SID {sid}: {e}"
            )
        return
    existing_sid = None
    for s, info in list(_sid_registry.items()):
        if info.get("workshop_id") == workshop_id and info.get("user_id") == user_id:
            existing_sid = s
            break
    if existing_sid and existing_sid != sid:
        current_app.logger.warning(
            f"User {user_id} already in room {room} with SID {existing_sid}. Removing old entry."
        )
        _sid_registry.pop(existing_sid, None)
        if room in _room_presence:
            _room_presence[room].discard(user_id)
    join_room(room)
    _sid_registry[sid] = {
        "room": room,
        "workshop_id": workshop_id,
        "user_id": user_id,
    }
    if room not in _room_presence:
        _room_presence[room] = set()
    _room_presence[room].add(user_id)
    current_app.logger.info(f"User {user_id} (SID: {sid}) joined {room}")
    _broadcast_participant_list(room, workshop_id)
    initialize_participant_tracking(workshop_id, user_id)
    try:
        workshop = db.session.get(Workshop, workshop_id)
        if not workshop:
            return
        emit(
            "workshop_status_update",
            {"workshop_id": workshop_id, "status": workshop.status},
            to=sid,
        )
        if workshop.current_task_id and workshop.current_task:
            task = workshop.current_task
            remaining_seconds = workshop.get_remaining_task_time()
            current_task_index = (
                workshop.current_task_index
                if workshop.current_task_index is not None
                else -1
            )
            current_task_type = (
                (task.task_type or "").strip().lower()
                if getattr(task, "task_type", None)
                else ""
            )
            if not current_task_type:
                try:
                    ordered_items = (
                        WorkshopPlanItem.query.filter_by(
                            workshop_id=workshop_id, enabled=True
                        )
                        .order_by(WorkshopPlanItem.order_index.asc())
                        .all()
                    )
                    if 0 <= current_task_index < len(ordered_items):
                        current_task_type = (
                            ordered_items[current_task_index].task_type or ""
                        ).strip().lower() or "unknown"
                    else:
                        current_task_type = (
                            TASK_SEQUENCE[current_task_index]
                            if 0 <= current_task_index < len(TASK_SEQUENCE)
                            else "unknown"
                        )
                except Exception:  # noqa
                    current_task_type = (
                        TASK_SEQUENCE[current_task_index]
                        if 0 <= current_task_index < len(TASK_SEQUENCE)
                        else "unknown"
                    )
            if current_task_index == -1:
                current_task_type = "warm-up"
            current_app.logger.debug(
                f"Syncing state for task {task.id} (Type: {current_task_type}, Index: {current_task_index})"
            )
            task_details: dict[str, Any] = {}
            # Prefer payload_json (may include server-augmented fields like feasibility_pdf_url)
            raw_json: str | None = None
            try:
                raw_json = getattr(task, 'payload_json', None) or getattr(task, 'prompt', None)
            except Exception:
                raw_json = getattr(task, 'prompt', None)
            try:
                task_details = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                current_app.logger.warning(
                    f"Could not parse task JSON for task {task.id}"
                )
                task_details = {"error": "Could not load task details."}
            event_name = "task_ready"
            payload = {
                "task_id": task.id,
                "title": task.title,
                "duration": task.duration,
                "task_type": current_task_type,
                "task_index": current_task_index,
                **task_details,
            }
            if current_task_type == "warm-up":
                event_name = "warm_up_start"
            elif current_task_type == "clustering_voting":
                event_name = "clusters_ready"
                participants_data = WorkshopParticipant.query.filter_by(
                    workshop_id=workshop_id, status="accepted"
                ).all()
                payload["participants_dots"] = {
                    part.user_id: part.dots_remaining for part in participants_data
                }
                try:
                    # Expose configured dots per user for clearer client instructions
                    payload["dots_per_user"] = int(getattr(workshop, "dots_per_user", 5) or 5)
                except Exception:
                    payload["dots_per_user"] = 5
            elif current_task_type == "results_feasibility":
                event_name = "feasibility_ready"
            elif current_task_type == "results_prioritization":
                event_name = "prioritization_ready"
            elif current_task_type == "results_action_plan":
                event_name = "action_plan_ready"
            elif current_task_type == "summary":
                event_name = "summary_ready"
            elif current_task_type == "discussion":
                event_name = "discussion_ready"
            elif current_task_type == "vote_generic":
                event_name = "vote_ready"
            current_app.logger.debug(
                f"Emitting {event_name} to {sid} for task {task.id}"
            )
            emit(event_name, payload, to=sid)
            # Emit any persisted UI flags (e.g., organizer toggles) to late joiners
            try:
                wid = int(workshop_id)
                flags = _ui_flags.get(wid, {})
                for k, v in flags.items():
                    emit('ui_hint', { 'workshop_id': wid, 'key': k, 'value': v }, to=sid)
            except Exception:
                pass
            # If current task is presentation, also send last-known viewer state so late joiners align
            try:
                if current_task_type == "presentation":
                    key = (int(workshop_id), int(task.id))
                    st = _presentation_state.get(key)
                    if st:
                        emit("presentation_sync", st, to=sid)
                elif current_task_type == "results_feasibility":
                    # Emit last-known feasibility viewer state (page/zoom/fit) if any
                    key = (int(workshop_id), int(task.id))
                    fst = _feasibility_state.get(key)
                    if fst:
                        emit("feasibility_sync", fst, to=sid)
                elif current_task_type == "results_prioritization":
                    key = (int(workshop_id), int(task.id))
                    pst = _prioritization_state.get(key)
                    if pst:
                        emit("prioritization_sync", pst, to=sid)
                elif current_task_type == "results_action_plan":
                    key = (int(workshop_id), int(task.id))
                    ast = _action_plan_state.get(key)
                    if ast:
                        emit("action_plan_sync", ast, to=sid)
            except Exception:
                pass
            try:
                wid_int = int(workshop_id) if workshop_id is not None else None
            except (TypeError, ValueError):
                wid_int = None
            emit_timer_sync(
                sid,
                {
                    "task_id": task.id,
                    "remaining_seconds": remaining_seconds,
                    "is_paused": workshop.status == "paused",
                },
                workshop_id=wid_int,
            )
            if current_task_type in ["warm-up", "brainstorming", "discussion"]:
                include_flag = True
                try:
                    payload_blob = json.loads(task.payload_json) if task.payload_json else {}
                    if isinstance(payload_blob, dict):
                        include_flag = bool(payload_blob.get("ai_ideas_include_in_outputs", True))
                except Exception:
                    include_flag = True
                ideas = (
                    BrainstormIdea.query.filter_by(task_id=task.id)
                    .order_by(BrainstormIdea.timestamp)
                    .all()
                )
                ideas_payload = []
                for idea in ideas:
                    try:
                        username = (
                            idea.participant.user.first_name
                            or idea.participant.user.email.split("@")[0]
                            if idea.participant and idea.participant.user
                            else "Unknown"
                        )
                    except Exception:
                        username = "Unknown"
                    metadata: Any = None
                    raw_meta = getattr(idea, "metadata_json", None)
                    if raw_meta:
                        try:
                            metadata = json.loads(raw_meta)
                        except Exception:
                            metadata = raw_meta
                    ideas_payload.append(
                        {
                            "idea_id": idea.id,
                            "user": username,
                            "content": idea.content,
                            "timestamp": idea.timestamp.isoformat(),
                            "source": getattr(idea, "source", "human"),
                            "rationale": getattr(idea, "rationale", None),
                            "metadata": metadata,
                            "include_in_outputs": bool(getattr(idea, "include_in_outputs", True)),
                        }
                    )
                emit(
                    "whiteboard_sync",
                    {"ideas": ideas_payload, "ai_ideas_include_in_outputs": include_flag},
                    to=sid,
                )
                current_app.logger.debug(
                    f"Emitted whiteboard_sync with {len(ideas_payload)} ideas to {sid}"
                )
            elif current_task_type == "clustering_voting":
                clusters_with_votes = (
                    db.session.query(
                        IdeaCluster, func.count(IdeaVote.id).label("vote_count")
                    )
                    .outerjoin(IdeaVote, IdeaCluster.id == IdeaVote.cluster_id)
                    .filter(IdeaCluster.task_id == task.id)
                    .group_by(IdeaCluster.id)
                    .all()
                )
                votes_payload = {cluster.id: count for cluster, count in clusters_with_votes}
                emit("all_votes_sync", {"votes": votes_payload}, to=sid)
                current_app.logger.debug(
                    f"Emitted all_votes_sync with counts for {len(votes_payload)} clusters to {sid}"
                )
                # Also emit the set of clusters this participant has already voted on
                try:
                    participant = WorkshopParticipant.query.filter_by(
                        workshop_id=workshop_id, user_id=user_id, status="accepted"
                    ).first()
                    if participant:
                        voted_cluster_ids = (
                            db.session.query(IdeaVote.cluster_id)
                            .join(IdeaCluster, IdeaCluster.id == IdeaVote.cluster_id)
                            .filter(
                                IdeaVote.participant_id == participant.id,
                                IdeaCluster.task_id == task.id,
                            )
                            .all()
                        )
                        voted_cluster_ids = [cid for (cid,) in voted_cluster_ids]
                        emit("user_votes_sync", {"voted_cluster_ids": voted_cluster_ids}, to=sid)
                        current_app.logger.debug(
                            f"Emitted user_votes_sync with {len(voted_cluster_ids)} clusters for participant {participant.id}"
                        )
                except Exception as e:  # noqa
                    current_app.logger.warning(
                        f"Failed emitting user_votes_sync for workshop {workshop_id}, user {user_id}: {e}"
                    )
            elif current_task_type == "vote_generic":
                # Sync generic vote counts and user's voted items
                try:
                    # Parse task prompt/payload items
                    items: list[Any] = []
                    try:
                        details = json.loads(task.prompt) if task.prompt else {}
                        if isinstance(details, dict):
                            raw_items = details.get('items')
                            if isinstance(raw_items, list):
                                items = raw_items
                    except Exception:
                        items = []
                    # Count votes per (item_type,item_id) for this task
                    rows = (
                        db.session.query(GenericVote.item_type, GenericVote.item_id, func.count(GenericVote.id))
                        .filter(GenericVote.task_id == task.id)
                        .group_by(GenericVote.item_type, GenericVote.item_id)
                        .all()
                    )
                    counts = {f"{t}:{i}": c for (t, i, c) in rows}
                    emit("generic_votes_sync", {"counts": counts}, to=sid)
                    # User's selected items
                    participant = WorkshopParticipant.query.filter_by(
                        workshop_id=workshop_id, user_id=user_id, status="accepted"
                    ).first()
                    if participant:
                        mine = (
                            db.session.query(GenericVote.item_type, GenericVote.item_id)
                            .filter(GenericVote.task_id == task.id, GenericVote.participant_id == participant.id)
                            .all()
                        )
                        mine_keys = [f"{t}:{i}" for (t, i) in mine]
                        emit("generic_user_votes_sync", {"items": mine_keys}, to=sid)
                except Exception as e:
                    current_app.logger.warning(f"Failed generic vote sync for workshop {workshop_id}: {e}")
        else:
            current_app.logger.debug(
                f"Workshop {workshop_id} has no active task upon join."
            )
            emit("no_active_task", {}, to=sid)
        # Optional chat scope filter (defaults to 'workshop_chat')
        scope = data.get('scope') or 'workshop_chat'
        q = ChatMessage.query.filter_by(workshop_id=workshop_id)
        try:
            if scope in ('workshop_chat', 'discussion_chat'):
                q = q.filter(ChatMessage.chat_scope == scope)  # type: ignore[attr-defined]
        except Exception:
            pass
        chat_history = (
            q
            .order_by(ChatMessage.timestamp.desc())
            .limit(50)
            .all()
        )
        chat_history.reverse()
        history_payload = []
        for msg in chat_history:
            try:
                mtype = getattr(msg, 'message_type', 'user')
            except Exception:
                mtype = 'user'
            try:
                cscope = getattr(msg, 'chat_scope', 'workshop_chat')
            except Exception:
                cscope = 'workshop_chat'
            history_payload.append({
                "user_name": msg.username,
                "message": msg.message,
                "timestamp": msg.timestamp.isoformat(),
                "message_type": mtype,
                "chat_scope": cscope,
            })
        emit("chat_history", {"messages": history_payload}, to=sid)
    except Exception as e:  # noqa
        current_app.logger.error(
            f"Error during join_room state emission for workshop {workshop_id}, SID {sid}: {e}",
            exc_info=True,
        )
        emit("error_joining", {"message": "Error retrieving workshop state."}, to=sid)


@socketio.on("leave_room")
def _on_leave_room(data):  # type: ignore
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    sid = getattr(request, "sid", None) if has_request_context() else None
    if not all([room, workshop_id, user_id]):
        current_app.logger.warning(f"leave_room incomplete data from {sid}: {data}")
        return
    leave_room(room)
    if room in _room_presence:
        _room_presence[room].discard(user_id)
    if sid in _sid_registry:
        _sid_registry.pop(sid)
        if workshop_id and user_id:
            cleanup_participant_tracking(workshop_id, user_id)
        current_app.logger.info(f"User {user_id} (SID: {sid}) left {room}")
    else:
        current_app.logger.warning(
            f"SID {sid} emitted leave_room but was not in registry for room {room}."
        )
    if room in _room_presence and _room_presence[room]:
        _broadcast_participant_list(room, workshop_id)
    elif room in _room_presence:
        del _room_presence[room]


@socketio.on("request_participant_list")
def _on_request_participant_list(data):  # type: ignore
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    if not all([room, workshop_id]):
        return
    _broadcast_participant_list(room, workshop_id)


@socketio.on("send_message")
def _on_send_message(data):  # type: ignore
    room = data.get("room")
    message = data.get("message", "").strip()
    user_id = data.get("user_id")
    workshop_id = data.get("workshop_id")
    if not all([room, message, user_id, workshop_id]):
        return
    user = db.session.get(User, user_id)
    if not user:
        return
    workshop = db.session.get(Workshop, workshop_id)
    if not workshop or workshop.status not in [
        "inprogress",
        "paused",
        "scheduled",
    ]:
        current_app.logger.warning(
            f"Chat message attempt in inactive workshop {workshop_id}"
        )
        return
    username = user.first_name or user.email.split("@")[0]
    try:
        chat_message = ChatMessage()
        chat_message.workshop_id = workshop_id
        chat_message.user_id = user_id
        chat_message.username = username
        chat_message.message = message
        chat_message.timestamp = datetime.utcnow()
        # Tag as a user-originated chat message
        try:
            chat_message.message_type = 'user'
        except Exception:
            pass
        # Assign scope (default to 'workshop_chat' unless provided)
        try:
            scope = data.get('scope') or 'workshop_chat'
            if scope not in ('workshop_chat', 'discussion_chat'):
                scope = 'workshop_chat'
            setattr(chat_message, 'chat_scope', scope)
        except Exception:
            pass
        db.session.add(chat_message)
        db.session.commit()
        emit(
            "receive_message",
            {
                "user_name": chat_message.username,
                "message": chat_message.message,
                "timestamp": chat_message.timestamp.isoformat(),
                "message_type": getattr(chat_message, 'message_type', 'user'),
                "chat_scope": getattr(chat_message, 'chat_scope', 'workshop_chat'),
                "room": room,
            },
            to=room,
        )
    except Exception as e:  # noqa
        db.session.rollback()
        current_app.logger.error(
            f"Error saving chat message for workshop {workshop_id}: {e}"
        )
        try:
            sid = getattr(request, "sid", None) if has_request_context() else None
            emit("chat_error", {"message": "Failed to send message."}, to=sid or room)
        except Exception:
            pass


def emit_warm_up_start(room: str, payload: dict):  # type: ignore
    socketio.emit("warm_up_start", payload, to=room)
    current_app.logger.info(f"Emitted warm_up_start to {room}")


def emit_task_ready(room: str, payload: dict):  # type: ignore
    socketio.emit("task_ready", payload, to=room)
    current_app.logger.info(
        f"Emitted task_ready to {room} for task {payload.get('task_id')}"
    )

# --- Presentation viewer state sync ---
@socketio.on("presentation_control")
def _on_presentation_control(data):  # type: ignore
    """Organizer or presenter can control the presentation viewer.
    Expects: { room, workshop_id, user_id, task_id, action: 'goto'|'zoom'|'fit', page?, zoom? }
    Broadcasts 'presentation_sync' with the normalized state so late joiners can restore.
    """
    try:
        room = data.get("room")
        workshop_id = int(data.get("workshop_id")) if data.get("workshop_id") else None
        user_id = int(data.get("user_id")) if data.get("user_id") else None
        task_id = int(data.get("task_id")) if data.get("task_id") else None
        action = (data.get("action") or "").strip().lower()
        if not all([room, workshop_id, user_id, task_id, action]):
            return
        ws = db.session.get(Workshop, workshop_id)
        if not ws or ws.current_task_id != task_id:
            return
        # Permission: only organizer or configured presenter may control
        is_org = (ws.created_by_id == user_id)
        is_presenter = False
        try:
            task = db.session.get(BrainstormTask, task_id)
            cfg = json.loads(task.prompt) if (task and task.prompt) else {}
            pres_id = int(cfg.get("presenter_user_id") or 0)
            is_presenter = (pres_id != 0) and (pres_id == user_id)
        except Exception:
            is_presenter = False
        if not (is_org or is_presenter):
            current_app.logger.info(f"presentation_control denied for user {user_id} in ws {workshop_id}")
            return

        # Normalize state payload
        page = int(data.get("page") or 1)
        zoom = float(data.get("zoom") or 1.0)
        fit = (data.get("fit") or "")
        payload = {
            "workshop_id": workshop_id,
            "task_id": task_id,
            "action": action,
            "page": max(1, page),
            "zoom": max(0.25, min(5.0, zoom)),
            "fit": fit if fit in ("page","width","height","auto","none") else "none",
            "by": user_id,
            "ts": datetime.utcnow().isoformat(),
        }
        # Remember last-known state for late joiners
        try:
            wid = int(workshop_id or 0)
            tid = int(task_id or 0)
            if wid and tid:
                _presentation_state[(wid, tid)] = dict(payload)
        except Exception:
            pass
        socketio.emit("presentation_sync", payload, to=room)
    except Exception as e:  # noqa
        current_app.logger.warning(f"presentation_control error: {e}")

# --- Feasibility viewer state sync ---
@socketio.on("feasibility_control")
def _on_feasibility_control(data):  # type: ignore
    """Organizer or presenter can control the feasibility viewer.
    Expects: { room, workshop_id, user_id, task_id, action: 'goto'|'zoom'|'fit', page?, zoom? }
    Broadcasts 'feasibility_sync' with the normalized state so late joiners can restore.
    Only allowed when the current task is results_feasibility.
    """
    try:
        room = data.get("room")
        workshop_id = int(data.get("workshop_id")) if data.get("workshop_id") else None
        user_id = int(data.get("user_id")) if data.get("user_id") else None
        task_id = int(data.get("task_id")) if data.get("task_id") else None
        action = (data.get("action") or "").strip().lower()
        if not all([room, workshop_id, user_id, task_id, action]):
            return
        ws = db.session.get(Workshop, workshop_id)
        if not ws or ws.current_task_id != task_id:
            return
        # Only for feasibility task
        task = db.session.get(BrainstormTask, task_id)
        current_type = (task.task_type or '').strip().lower() if task and task.task_type else ''
        if current_type != 'results_feasibility':
            return
        # Permission: only organizer or configured presenter may control
        is_org = (ws.created_by_id == user_id)
        is_presenter = False
        try:
            cfg = json.loads(task.prompt) if (task and task.prompt) else {}
            pres_id = int(cfg.get("presenter_user_id") or 0)
            is_presenter = (pres_id != 0) and (pres_id == user_id)
        except Exception:
            is_presenter = False
        if not (is_org or is_presenter):
            current_app.logger.info(f"feasibility_control denied for user {user_id} in ws {workshop_id}")
            return

        # Normalize state payload
        page = int(data.get("page") or 1)
        zoom = float(data.get("zoom") or 1.0)
        fit = (data.get("fit") or "")
        payload = {
            "workshop_id": workshop_id,
            "task_id": task_id,
            "action": action,
            "page": max(1, page),
            "zoom": max(0.25, min(5.0, zoom)),
            "fit": fit if fit in ("page","width","height","auto","none") else "none",
            "by": user_id,
            "ts": datetime.utcnow().isoformat(),
        }
        # Remember last-known state for late joiners
        try:
            wid = int(workshop_id or 0)
            tid = int(task_id or 0)
            if wid and tid:
                _feasibility_state[(wid, tid)] = dict(payload)
        except Exception:
            pass
        socketio.emit("feasibility_sync", payload, to=room)
    except Exception as e:  # noqa
        current_app.logger.warning(f"feasibility_control error: {e}")

# --- Prioritization (shortlisting) viewer state sync ---
@socketio.on("prioritization_control")
def _on_prioritization_control(data):  # type: ignore
    """Control the prioritization (shortlist) PDF viewer.
    Expects: { room, workshop_id, user_id, task_id, action: 'goto'|'zoom'|'fit', page?, zoom? }
    Only allowed when the current task is results_prioritization.
    """
    try:
        room = data.get("room")
        workshop_id = int(data.get("workshop_id")) if data.get("workshop_id") else None
        user_id = int(data.get("user_id")) if data.get("user_id") else None
        task_id = int(data.get("task_id")) if data.get("task_id") else None
        action = (data.get("action") or "").strip().lower()
        if not all([room, workshop_id, user_id, task_id, action]):
            return
        ws = db.session.get(Workshop, workshop_id)
        if not ws or ws.current_task_id != task_id:
            return
        task = db.session.get(BrainstormTask, task_id)
        current_type = (task.task_type or '').strip().lower() if task and task.task_type else ''
        if current_type != 'results_prioritization':
            return
        is_org = (ws.created_by_id == user_id)
        is_presenter = False
        try:
            cfg = json.loads(task.prompt) if (task and task.prompt) else {}
            pres_id = int(cfg.get("presenter_user_id") or 0)
            is_presenter = (pres_id != 0) and (pres_id == user_id)
        except Exception:
            is_presenter = False
        if not (is_org or is_presenter):
            return
        page = int(data.get("page") or 1)
        zoom = float(data.get("zoom") or 1.0)
        fit = (data.get("fit") or "")
        payload = {
            "workshop_id": workshop_id,
            "task_id": task_id,
            "action": action,
            "page": max(1, page),
            "zoom": max(0.25, min(5.0, zoom)),
            "fit": fit if fit in ("page","width","height","auto","none") else "none",
            "by": user_id,
            "ts": datetime.utcnow().isoformat(),
        }
        try:
            wk_id = int(workshop_id) if workshop_id is not None else None
            tk_id = int(task_id) if task_id is not None else None
            if wk_id is not None and tk_id is not None:
                _prioritization_state[(wk_id, tk_id)] = dict(payload)
        except Exception:
            pass
        socketio.emit("prioritization_sync", payload, to=room)
    except Exception as e:  # noqa
        current_app.logger.warning(f"prioritization_control error: {e}")

# --- Action Plan viewer state sync ---
@socketio.on("action_plan_control")
def _on_action_plan_control(data):  # type: ignore
    """Control the action plan PDF viewer.
    Expects: { room, workshop_id, user_id, task_id, action: 'goto'|'zoom'|'fit', page?, zoom? }
    Only allowed when the current task is results_action_plan.
    """
    try:
        room = data.get("room")
        workshop_id = int(data.get("workshop_id")) if data.get("workshop_id") else None
        user_id = int(data.get("user_id")) if data.get("user_id") else None
        task_id = int(data.get("task_id")) if data.get("task_id") else None
        action = (data.get("action") or "").strip().lower()
        if not all([room, workshop_id, user_id, task_id, action]):
            return
        ws = db.session.get(Workshop, workshop_id)
        if not ws or ws.current_task_id != task_id:
            return
        task = db.session.get(BrainstormTask, task_id)
        current_type = (task.task_type or '').strip().lower() if task and task.task_type else ''
        if current_type != 'results_action_plan':
            return
        is_org = (ws.created_by_id == user_id)
        is_presenter = False
        try:
            cfg = json.loads(task.prompt) if (task and task.prompt) else {}
            pres_id = int(cfg.get("presenter_user_id") or 0)
            is_presenter = (pres_id != 0) and (pres_id == user_id)
        except Exception:
            is_presenter = False
        if not (is_org or is_presenter):
            return
        page = int(data.get("page") or 1)
        zoom = float(data.get("zoom") or 1.0)
        fit = (data.get("fit") or "")
        payload = {
            "workshop_id": workshop_id,
            "task_id": task_id,
            "action": action,
            "page": max(1, page),
            "zoom": max(0.25, min(5.0, zoom)),
            "fit": fit if fit in ("page","width","height","auto","none") else "none",
            "by": user_id,
            "ts": datetime.utcnow().isoformat(),
        }
        try:
            wk_id = int(workshop_id) if workshop_id is not None else None
            tk_id = int(task_id) if task_id is not None else None
            if wk_id is not None and tk_id is not None:
                _action_plan_state[(wk_id, tk_id)] = dict(payload)
        except Exception:
            pass
        socketio.emit("action_plan_sync", payload, to=room)
    except Exception as e:  # noqa
        current_app.logger.warning(f"action_plan_control error: {e}")

@socketio.on("facilitator_tts_event")
def _on_facilitator_tts_event(data):  # type: ignore
    """Persist a facilitator transcript entry on play so the transcript updates during narration.
    Expects payload: { kind: 'play'|'pause'|'stop'|'ended', workshop_id, task_id, text? }
    """
    try:
        kind = (data or {}).get('kind')
        workshop_id = (data or {}).get('workshop_id')
        task_id = (data or {}).get('task_id')
        text = (data or {}).get('text') or ''
        partial = (data or {}).get('partial') or ''
        if not workshop_id:
            return
        room = f"workshop_room_{workshop_id}"
        # Track facilitator playback state for server-side STT suppression
        try:
            from app.sockets.state import set_facilitator_playback, touch_facilitator_playback, clear_facilitator_playback
        except Exception:
            set_facilitator_playback = touch_facilitator_playback = clear_facilitator_playback = None  # type: ignore

        # Stream partials while audio plays so UI can render live words
        if kind == 'progress' and partial:
            try:
                # Attribute partials to the AI Facilitator user for consistency
                try:
                    from app.workshop.helpers import get_or_create_facilitator_user
                    fac_user = get_or_create_facilitator_user()
                    uid = int(getattr(fac_user, 'user_id', 0) or 0)
                except Exception:
                    uid = 0
                if touch_facilitator_playback is not None:
                    touch_facilitator_playback(int(workshop_id))
                socketio.emit('stt_partial', {
                    'workshop_id': int(workshop_id),
                    'user_id': uid,
                    'entry_type': 'facilitator',
                    'text': str(partial),
                }, to=room)
            except Exception:
                pass
            return

        # Handle pause/stop to update playback state and optionally clear partials
        if kind in ('pause', 'stop'):
            try:
                if set_facilitator_playback is not None:
                    set_facilitator_playback(int(workshop_id), active=False, task_id=int(task_id) if task_id else None)
                # Clear any lingering partials on clients when stopping
                if kind == 'stop':
                    try:
                        from app.workshop.helpers import get_or_create_facilitator_user
                        fac_user = get_or_create_facilitator_user()
                        uid = int(getattr(fac_user, 'user_id', 0) or 0)
                    except Exception:
                        uid = 0
                    socketio.emit('stt_partial', {
                        'workshop_id': int(workshop_id),
                        'user_id': uid,
                        'entry_type': 'facilitator',
                        'text': '',  # signal to remove
                    }, to=room)
            except Exception:
                pass
            return

        # Create/persist on first play but do not emit final yet (final is sent on 'ended')
        if kind == 'play' and text.strip():
            ws = db.session.get(Workshop, int(workshop_id))
            if not ws:
                return
            # Use the designated AI Facilitator system user for attribution
            try:
                from app.workshop.helpers import get_or_create_facilitator_user
                fac_user = get_or_create_facilitator_user()
                user_id = int(getattr(fac_user, 'user_id', 0) or 0)
                first = getattr(fac_user, 'first_name', None)
                last = getattr(fac_user, 'last_name', None)
            except Exception:
                user_id = 0
                first = None
                last = None
            if set_facilitator_playback is not None:
                set_facilitator_playback(int(workshop_id), active=True, task_id=int(task_id) if task_id else None)
            # Idempotent per (workshop_id, task_id): update existing or create new
            try:
                existing = None
                if task_id is not None:
                    try:
                        existing = Transcript.query.filter(
                            Transcript.workshop_id == int(workshop_id),
                            Transcript.entry_type == 'facilitator',
                            Transcript.task_id == int(task_id),
                        ).order_by(Transcript.created_timestamp.desc()).first()
                    except Exception:
                        existing = None
                if existing is None:
                    # Fallback: try match by same text + user within this workshop
                    try:
                        filters = [
                            Transcript.workshop_id == int(workshop_id),
                            Transcript.entry_type == 'facilitator',
                            Transcript.raw_stt_transcript == text.strip(),
                        ]
                        if user_id:
                            filters.append(Transcript.user_id == int(user_id))
                        existing = Transcript.query.filter(*filters).order_by(Transcript.created_timestamp.desc()).first()
                    except Exception:
                        existing = None
                if existing:
                    # Update text/start if needed
                    try:
                        if (existing.raw_stt_transcript or '') != text.strip():
                            existing.raw_stt_transcript = text.strip()
                    except Exception:
                        pass
                    try:
                        if not getattr(existing, 'start_timestamp', None):
                            existing.start_timestamp = datetime.utcnow()
                    except Exception:
                        pass
                    db.session.commit()
                    return
                # Create new facilitator transcript row for this task
                t = Transcript()
                t.workshop_id = int(workshop_id)
                t.user_id = int(user_id) if user_id else 0
                try:
                    if task_id is not None:
                        setattr(t, 'task_id', int(task_id))
                except Exception:
                    pass
                try:
                    setattr(t, 'entry_type', 'facilitator')
                except Exception:
                    pass
                try:
                    setattr(t, 'raw_stt_transcript', text.strip())
                except Exception:
                    pass
                try:
                    setattr(t, 'processed_transcript', None)
                    setattr(t, 'language', None)
                except Exception:
                    pass
                try:
                    setattr(t, 'start_timestamp', datetime.utcnow())
                    setattr(t, 'end_timestamp', None)
                    setattr(t, 'confidence', None)
                except Exception:
                    pass
                db.session.add(t)
                db.session.commit()
                return
            except Exception:
                db.session.rollback()
                raise

        # On ended: finalize/update the persisted facilitator transcript and emit final with transcript_id
        if kind == 'ended' and text.strip():
            try:
                ws = db.session.get(Workshop, int(workshop_id))
                # Use designated AI Facilitator user for attribution
                try:
                    from app.workshop.helpers import get_or_create_facilitator_user
                    fac_user = get_or_create_facilitator_user()
                    user_id = int(getattr(fac_user, 'user_id', 0) or 0)
                    first = getattr(fac_user, 'first_name', None)
                    last = getattr(fac_user, 'last_name', None)
                except Exception:
                    user_id = 0
                    first = None
                    last = None
                # Try to locate existing facilitator transcript for this task first
                recent = None
                if task_id is not None:
                    try:
                        recent = Transcript.query.filter(
                            Transcript.workshop_id == int(workshop_id),
                            Transcript.entry_type == 'facilitator',
                            Transcript.task_id == int(task_id),
                        ).order_by(Transcript.created_timestamp.desc()).first()
                    except Exception:
                        recent = None
                if recent is None:
                    # Fallback: match by same text + user within this workshop
                    filters = [
                        Transcript.workshop_id == int(workshop_id),
                        Transcript.entry_type == 'facilitator',
                        Transcript.raw_stt_transcript == text.strip(),
                    ]
                    if user_id:
                        filters.append(Transcript.user_id == int(user_id))
                    recent = Transcript.query.filter(*filters).order_by(Transcript.created_timestamp.desc()).first()
                if not recent:
                    # If not found (e.g., page refreshed before play insert), create now
                    recent = Transcript()
                    recent.workshop_id = int(workshop_id)
                    recent.user_id = int(user_id) if user_id else 0
                    try:
                        if task_id is not None:
                            setattr(recent, 'task_id', int(task_id))
                    except Exception:
                        pass
                    try:
                        setattr(recent, 'entry_type', 'facilitator')
                    except Exception:
                        pass
                    try:
                        setattr(recent, 'raw_stt_transcript', text.strip())
                    except Exception:
                        pass
                    try:
                        setattr(recent, 'start_timestamp', datetime.utcnow())
                    except Exception:
                        pass
                    db.session.add(recent)
                # Finalize end_timestamp
                try:
                    setattr(recent, 'end_timestamp', datetime.utcnow())
                except Exception:
                    pass
                # Ensure text is current
                try:
                    if (recent.raw_stt_transcript or '') != text.strip():
                        recent.raw_stt_transcript = text.strip()
                except Exception:
                    pass
                db.session.commit()
                if set_facilitator_playback is not None:
                    set_facilitator_playback(int(workshop_id), active=False, task_id=int(task_id) if task_id else None)
                # Emit final with persisted transcript_id so UI can de-dup/update in place
                # 'first' and 'last' already resolved above from facilitator user; keep as-is
                # Safe datetime formatting
                _st = getattr(recent, 'start_timestamp', None)
                _et = getattr(recent, 'end_timestamp', None)
                socketio.emit('transcript_final', {
                    'workshop_id': int(workshop_id),
                    'transcript_id': int(getattr(recent, 'transcript_id', 0) or 0),
                    'user_id': int(user_id) if user_id else 0,
                    'first_name': first,
                    'last_name': last,
                    'entry_type': 'facilitator',
                    'task_id': int(task_id) if task_id else None,
                    'text': text.strip(),
                    'startTs': _st.isoformat() if _st else None,
                    'endTs': _et.isoformat() if _et else datetime.utcnow().isoformat(),
                }, to=room)
            except Exception as _e:
                db.session.rollback()
                raise
            return
    except Exception as e:  # noqa
        db.session.rollback()
        current_app.logger.error(f"facilitator_tts_event error: {e}")

@socketio.on("submit_vote")
def _on_submit_vote(data):  # type: ignore
    """Handle a participant casting a dot vote on a cluster during clustering_voting."""
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    cluster_id = data.get("cluster_id")
    sid = getattr(request, "sid", None) if has_request_context() else None
    if not all([room, workshop_id, user_id, cluster_id]):
        current_app.logger.warning(f"submit_vote incomplete data from {sid}: {data}")
        return
    try:
        workshop = db.session.get(Workshop, int(workshop_id))
        # Only allow voting when the workshop is actively in progress (reject when paused)
        if not workshop or workshop.status not in [
            "inprogress",
        ]:
            current_app.logger.warning(
                f"Vote attempt in inactive workshop {workshop_id}"
            )
            return
        if not workshop.current_task_id:
            current_app.logger.warning(
                f"Vote attempt with no active task in workshop {workshop_id}"
            )
            return
        task = db.session.get(BrainstormTask, workshop.current_task_id)
        if not task or task.task_type != "clustering_voting":
            current_app.logger.warning(
                f"Vote attempt when current task is not clustering_voting (task {getattr(task,'id',None)} type {getattr(task,'task_type',None)})"
            )
            return
        participant = WorkshopParticipant.query.filter_by(
            workshop_id=workshop.id, user_id=int(user_id), status="accepted"
        ).first()
        if not participant:
            current_app.logger.warning(
                f"submit_vote: user {user_id} not an accepted participant in workshop {workshop.id}"
            )
            return
        if participant.dots_remaining is None:
            participant.dots_remaining = 0
        if participant.dots_remaining <= 0:
            current_app.logger.info(
                f"submit_vote: user {user_id} has no dots remaining"
            )
            # Optionally notify user
            socketio.emit("vote_update", {
                "cluster_id": int(cluster_id),
                "total_votes": IdeaVote.query.filter_by(cluster_id=int(cluster_id)).count(),
                "user_id": int(user_id),
                "dots_remaining": 0,
                "action_taken": "none"
            }, to=sid or room)
            return
        cluster = IdeaCluster.query.filter_by(
            id=int(cluster_id), task_id=task.id
        ).first()
        if not cluster:
            current_app.logger.warning(
                f"submit_vote: cluster {cluster_id} not found for task {task.id}"
            )
            return
        # Enforce one vote per cluster per participant (unique constraint also enforces)
        existing = IdeaVote.query.filter_by(
            cluster_id=cluster.id, participant_id=participant.id
        ).first()
        if existing:
            # Toggle off: remove vote and restore one dot (capped to dots_per_user)
            try:
                db.session.delete(existing)
                # Restore a dot, but don't exceed configured dots_per_user
                cap = int(getattr(workshop, "dots_per_user", 5) or 5)
                current = int(participant.dots_remaining or 0)
                participant.dots_remaining = min(cap, current + 1)
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            total = IdeaVote.query.filter_by(cluster_id=cluster.id).count()
            socketio.emit(
                "vote_update",
                {
                    "cluster_id": cluster.id,
                    "total_votes": total,
                    "user_id": int(user_id),
                    "dots_remaining": participant.dots_remaining,
                    "action_taken": "unvoted",
                },
                to=room,
            )
            return
        # Cast vote
        vote = IdeaVote()
        vote.cluster_id = cluster.id
        vote.participant_id = participant.id
        vote.dots_used = 1
        db.session.add(vote)
        # Decrement dots
        participant.dots_remaining = max(0, (participant.dots_remaining or 0) - 1)
        db.session.commit()
        total = IdeaVote.query.filter_by(cluster_id=cluster.id).count()
        socketio.emit(
            "vote_update",
            {
                "cluster_id": cluster.id,
                "total_votes": total,
                "user_id": int(user_id),
                "dots_remaining": participant.dots_remaining,
                "action_taken": "voted",
            },
            to=room,
        )
    except Exception as e:  # noqa
        db.session.rollback()
        current_app.logger.error(
            f"Error processing submit_vote for workshop {workshop_id}, user {user_id}, cluster {cluster_id}: {e}",
            exc_info=True,
        )
        socketio.emit(
            "vote_update",
            {
                "cluster_id": int(cluster_id),
                "total_votes": IdeaVote.query.filter_by(cluster_id=int(cluster_id)).count(),
                "user_id": int(user_id),
                "dots_remaining": 0,
                "action_taken": "error",
            },
            to=sid or room,
        )


def emit_workshop_stopped(room: str, workshop_id: int):  # type: ignore
    socketio.emit("workshop_stopped", {"workshop_id": workshop_id}, to=room)
    current_app.logger.info(f"Emitted workshop_stopped to {room}")


def emit_workshop_paused(room: str, workshop_id: int):  # type: ignore
    socketio.emit("workshop_paused", {"workshop_id": workshop_id}, to=room)
    current_app.logger.info(f"Emitted workshop_paused to {room}")


def emit_workshop_resumed(room: str, workshop_id: int):  # type: ignore
    socketio.emit("workshop_resumed", {"workshop_id": workshop_id}, to=room)
    current_app.logger.info(f"Emitted workshop_resumed to {room}")


def emit_timer_sync(room: str, payload: dict, *, workshop_id: Optional[int] = None):  # type: ignore
    socketio.emit("timer_sync", payload, to=room)
    current_app.logger.debug(f"Emitted timer_sync to {room}: {payload}")

    resolved_id = workshop_id if isinstance(workshop_id, int) else _extract_workshop_id(room, payload)
    if resolved_id is None:
        return
    try:
        emit_assistant_state(resolved_id)
    except Exception:
        current_app.logger.exception(
            "assistant_state_emit_failed_timer_sync",
            extra={"workshop_id": resolved_id, "room": room},
        )


def _extract_workshop_id(room: str, payload: dict) -> Optional[int]:
    candidates: List[Any] = []
    for key in ("workshop_id", "workshopId", "workshop"):
        if key in payload:
            candidates.append(payload.get(key))
    if not candidates and room:
        prefixes = ("workshop_room_", "workshop_", "room_")
        for prefix in prefixes:
            if room.startswith(prefix):
                candidates.append(room[len(prefix):])
                break
    for candidate in candidates:
        try:
            if candidate is None:
                continue
            if isinstance(candidate, str):
                stripped = candidate.strip()
                if not stripped:
                    continue
                return int(stripped)
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def emit_workshop_status_update(room: str, workshop_id: int, status: str):  # type: ignore
    socketio.emit(
        "workshop_status_update", {"workshop_id": workshop_id, "status": status}, to=room
    )
    current_app.logger.info(
        f"Emitted workshop_status_update ({status}) to {room}"
    )


@socketio.on("ui_hint")
def _on_ui_hint(data):  # type: ignore
    """Relay lightweight UI hints (e.g., organizer toggles) to all clients in the room.
    Expects: { room, workshop_id, user_id, key, value }
    Currently supports key == 'showRationaleAll' and restricts broadcasting to organizer.
    """
    try:
        room = data.get("room")
        raw_ws = data.get("workshop_id")
        raw_uid = data.get("user_id")
        workshop_id = int(raw_ws) if raw_ws is not None else None
        user_id = int(raw_uid) if raw_uid is not None else None
        key = (data.get("key") or "").strip()
        if not room or workshop_id is None or user_id is None or not key:
            return
        ws = db.session.get(Workshop, workshop_id)
        if not ws:
            return
        # Only organizer can toggle participant-visible rationale
        try:
            is_organizer = int(getattr(ws, 'created_by_id', 0) or 0) == int(user_id)
        except Exception:
            is_organizer = False
        if key == 'showRationaleAll' and not is_organizer:
            current_app.logger.info(f"ui_hint '{key}' denied for user {user_id} in ws {workshop_id}")
            return
        # Persist flag for late joiners (per-workshop)
        try:
            wid = int(workshop_id)
            val = data.get('value')
            # Normalize booleans for known keys
            if key == 'showRationaleAll':
                try:
                    val = bool(val)
                except Exception:
                    val = True if str(val).lower() in ('true','1','yes','on') else False
            _ui_flags[wid][key] = val
        except Exception:
            pass
        # Broadcast the hint to all clients in the room
        socketio.emit('ui_hint', {
            'workshop_id': workshop_id,
            'key': key,
            'value': data.get('value')
        }, to=room)
    except Exception as e:  # noqa
        current_app.logger.warning(f"ui_hint error: {e}")


@socketio.on("submit_vote_generic")
def _on_submit_vote_generic(data):  # type: ignore
    """Handle a participant casting a dot vote on a generic item during vote_generic.
    Expects: { room, workshop_id, user_id, item_key, item_type?, item_id? }
    item_key is preferred format `${type}:${id}` matching payload items.
    """
    room = data.get("room")
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    item_key = data.get("item_key")
    item_type = data.get("item_type")
    item_id = data.get("item_id")
    sid = getattr(request, "sid", None) if has_request_context() else None
    if not all([room, workshop_id, user_id]) or not (item_key or (item_type and item_id)):
        current_app.logger.warning(f"submit_vote_generic incomplete data from {sid}: {data}")
        return
    try:
        workshop = db.session.get(Workshop, int(workshop_id))
        if not workshop or workshop.status not in ["inprogress"]:
            return
        if not workshop.current_task_id:
            return
        task = db.session.get(BrainstormTask, workshop.current_task_id)
        if not task or task.task_type != "vote_generic":
            return
        # Resolve item_type/id
        if item_key and (":" in str(item_key)):
            parts = str(item_key).split(":", 1)
            item_type = item_type or parts[0]
            item_id = item_id or parts[1]
        if not (item_type and item_id):
            return
        # Validate participant
        participant = WorkshopParticipant.query.filter_by(
            workshop_id=workshop.id, user_id=int(user_id), status="accepted"
        ).first()
        if not participant:
            return
        if participant.dots_remaining is None:
            participant.dots_remaining = 0
        # Validate item exists in current task payload
        allowed: list[Any] = []
        try:
            payload = json.loads(task.prompt) if task.prompt else {}
            if isinstance(payload, dict):
                raw_allowed = payload.get('items')
                if isinstance(raw_allowed, list):
                    allowed = raw_allowed
        except Exception:
            allowed = []
        is_allowed = False
        for it in (allowed or []):
            try:
                t = str(it.get('type') or '').strip() if isinstance(it, dict) else ''
                i = str(it.get('id') or '').strip() if isinstance(it, dict) else ''
                if t == str(item_type) and i == str(item_id):
                    is_allowed = True
                    break
            except Exception:
                continue
        if not is_allowed:
            current_app.logger.info(f"submit_vote_generic: item not allowed {item_type}:{item_id}")
            return
        # Toggle if already voted
        existing = GenericVote.query.filter_by(
            task_id=task.id,
            participant_id=participant.id,
            item_type=str(item_type),
            item_id=str(item_id),
        ).first()
        if existing:
            try:
                db.session.delete(existing)
                # restore dot with cap
                cap = int(getattr(workshop, "dots_per_user", 5) or 5)
                participant.dots_remaining = min(cap, (participant.dots_remaining or 0) + 1)
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            total = (
                db.session.query(func.count(GenericVote.id))
                .filter_by(task_id=task.id, item_type=str(item_type), item_id=str(item_id))
                .scalar()
            ) or 0
            socketio.emit(
                "generic_vote_update",
                {
                    "item_key": f"{item_type}:{item_id}",
                    "total_votes": int(total),
                    "user_id": int(user_id),
                    "dots_remaining": int(participant.dots_remaining or 0),
                    "action_taken": "unvoted",
                },
                to=room,
            )
            return
        # New vote
        if (participant.dots_remaining or 0) <= 0:
            # Inform user of zero dots
            socketio.emit(
                "generic_vote_update",
                {
                    "item_key": f"{item_type}:{item_id}",
                    "total_votes": (
                        db.session.query(func.count(GenericVote.id))
                        .filter_by(task_id=task.id, item_type=str(item_type), item_id=str(item_id))
                        .scalar()
                    ) or 0,
                    "user_id": int(user_id),
                    "dots_remaining": 0,
                    "action_taken": "none",
                },
                to=sid or room,
            )
            return
        v = GenericVote()
        v.task_id = task.id
        v.participant_id = participant.id
        v.item_type = str(item_type)
        v.item_id = str(item_id)
        db.session.add(v)
        participant.dots_remaining = max(0, (participant.dots_remaining or 0) - 1)
        db.session.commit()
        total = (
            db.session.query(func.count(GenericVote.id))
            .filter_by(task_id=task.id, item_type=str(item_type), item_id=str(item_id))
            .scalar()
        ) or 0
        socketio.emit(
            "generic_vote_update",
            {
                "item_key": f"{item_type}:{item_id}",
                "total_votes": int(total),
                "user_id": int(user_id),
                "dots_remaining": int(participant.dots_remaining or 0),
                "action_taken": "voted",
            },
            to=room,
        )
    except Exception as e:  # noqa
        db.session.rollback()
        current_app.logger.error(
            f"Error processing submit_vote_generic for workshop {workshop_id}, user {user_id}, item {item_type}:{item_id}: {e}",
            exc_info=True,
        )
        try:
            socketio.emit(
                "generic_vote_update",
                {
                    "item_key": f"{item_type}:{item_id}",
                    "total_votes": 0,
                    "user_id": int(user_id) if user_id else None,
                    "dots_remaining": 0,
                    "action_taken": "error",
                },
                to=sid or room,
            )
        except Exception:
            pass


@socketio.on("forum_typing")
def _on_forum_typing(data):  # type: ignore
    """Broadcast typing indicators for forum posts within a topic.
    Expects: { room, workshop_id, user_id, topic_id, is_typing }
    """
    try:
        room = data.get("room")
        workshop_id = int(data.get("workshop_id")) if data.get("workshop_id") else None
        user_id = int(data.get("user_id")) if data.get("user_id") else None
        topic_id = int(data.get("topic_id")) if data.get("topic_id") else None
        is_typing = bool(data.get("is_typing"))
        if not all([room, workshop_id, user_id, topic_id]):
            return
        # Validate user belongs to workshop
        part = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, user_id=user_id).first()
        if not part:
            return
        socketio.emit(
            "forum_typing",
            {
                "workshop_id": workshop_id,
                "topic_id": topic_id,
                "user_id": user_id,
                "is_typing": is_typing,
            },
            to=room,
        )
    except Exception as e:  # noqa
        current_app.logger.warning(f"forum_typing error: {e}")
