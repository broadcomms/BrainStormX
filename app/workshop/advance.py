# app/workshop/advance.py
"""Server-side helper to advance a workshop to the next task and broadcast updates.
Decoupled from Flask blueprints and sockets module to avoid circular imports.
"""
import json
from datetime import datetime
from typing import Any, Dict, Tuple

from flask import current_app

from app.extensions import db, socketio
from app.models import Workshop, BrainstormTask, WorkshopPlanItem, Transcript
from app.config import TASK_SEQUENCE
from app.tasks.registry import TASK_REGISTRY
from app.tasks.validation import validate_payload
from app.workshop.helpers import get_or_create_facilitator_user
from app.sockets_core.core import emit_timer_sync
from app.assistant.assistant_socket import emit_assistant_state

# Import task payload generators
from app.service.routes.brainstorming import get_brainstorming_task_payload
from app.service.routes.warm_up import get_warm_up_payload
from app.service.routes import warm_up as warm_up_service
from app.service.routes.clustering import get_clustering_voting_payload
from app.service.routes.feasibility import get_feasibility_payload
from app.service.routes.discussion import get_discussion_payload
from app.service.routes.summary import get_summary_payload
from app.service.routes.meeting import get_meeting_payload
from app.service.routes.presentation import get_presentation_payload
from app.service.routes.speech import get_speech_payload
from app.service.routes.framing import get_framing_payload
from app.service.routes.vote_generic import get_vote_generic_payload
from app.service.routes.prioritization import get_prioritization_payload
from app.service.routes.action_plan import get_action_plan_payload


def _get_effective_task_sequence(workshop: Workshop):
    """Return only the task_type list for compatibility with existing callers.
    Uses DB-backed plan items if present, else falls back to JSON/TASK_SEQUENCE.
    """
    try:
        items = (
            WorkshopPlanItem.query
            .filter_by(workshop_id=workshop.id, enabled=True)
            .order_by(WorkshopPlanItem.order_index.asc())
            .all()
        )
        if items:
            return [it.task_type for it in items]
    except Exception:
        pass
    nodes = _get_plan_nodes(workshop)
    return [n["task_type"] for n in nodes]


def _normalize_duration(task_type: str, duration: int | None) -> int:
    """Return a safe integer duration (seconds) with registry default and guardrails.
    Note: Duration value 0 is treated as 'no override' in the plan; callers should handle 0 specially.
    """
    try:
        d = int(duration) if duration is not None else int(TASK_REGISTRY.get(task_type, {}).get("default_duration", 60))
    except Exception:
        d = int(TASK_REGISTRY.get(task_type, {}).get("default_duration", 60))
    # Guardrails: 30s .. 7200s (but permit 0 as sentinel when explicitly desired by caller)
    if d == 0:
        return 0
    d = max(30, min(7200, d))
    return d


def _emit_warm_up_completed(workshop: Workshop, task: BrainstormTask) -> None:
    try:
        cached_payload = warm_up_service.get_cached_warmup_payload(workshop.id)
    except Exception:
        cached_payload = None

    payload: dict | None
    if isinstance(cached_payload, dict):
        payload = dict(cached_payload)
    else:
        try:
            payload = json.loads(task.payload_json) if task.payload_json else {}
        except Exception:
            payload = {}

    if not isinstance(payload, dict):
        payload = {}

    event_payload = {
        "workshop_id": workshop.id,
        "task_id": task.id,
        "handoff_phrase": payload.get("handoff_phrase"),
        "next_phase": payload.get("next_phase"),
        "selected_option": payload.get("selected_option"),
        "selected_index": payload.get("selected_index"),
        "facilitator_panel": payload.get("facilitator_panel"),
        "completed_at": datetime.utcnow().isoformat() + "Z",
    }

    room = f"workshop_room_{workshop.id}"
    socketio.emit("warm_up_completed", event_payload, to=room)
    try:
        warm_up_service.clear_warmup_cache(workshop.id)
    except Exception:
        pass
    try:
        if current_app:
            current_app.logger.info(
                "[WarmUp] Emitted warm_up_completed for workshop %s", workshop.id
            )
    except Exception:
        pass


def _get_plan_nodes(workshop: Workshop):
    """Return a normalized list of plan nodes for this workshop.
    Each node: { task_type: str, duration: int, phase?: str, description?: str }
    Sources (priority order):
    1) DB-backed WorkshopPlanItem rows (preferred, preserves order_index).
    2) workshop.task_sequence JSON.
    3) Fallback to global TASK_SEQUENCE with registry defaults.
    Unknown task types are ignored. Disabled items are filtered out.
    """
    # 1) Prefer DB-backed plan items
    try:
        items = (
            WorkshopPlanItem.query
            .filter_by(workshop_id=workshop.id, enabled=True)
            .order_by(WorkshopPlanItem.order_index.asc())
            .all()
        )
        nodes = []
        for it in items:
            t = it.task_type
            cfg_obj = None
            raw_cfg = getattr(it, 'config_json', None) or getattr(it, 'description', None)
            if raw_cfg:
                try:
                    cfg_obj = json.loads(raw_cfg) if not isinstance(raw_cfg, dict) else raw_cfg  # type: ignore[arg-type]
                except Exception:
                    cfg_obj = None
            if t == 'speech' and isinstance(cfg_obj, dict) and (cfg_obj.get('delivery_mode') or '').strip().lower() == 'framing':
                t = 'framing'
            if t in TASK_REGISTRY:
                # Preserve 0 as "no override" sentinel; otherwise normalize
                dur = it.duration if (it.duration is not None and int(it.duration) == 0) else _normalize_duration(t, it.duration)
                nodes.append({
                    "task_type": t,
                    "duration": dur,
                    "phase": it.phase,
                    "description": it.description,
                })
        if nodes:
            return nodes
    except Exception:
        pass
    # Try to parse plan JSON on the workshop
    try:
        if workshop.task_sequence:
            raw = json.loads(workshop.task_sequence)
            parsed_nodes: list[dict[str, Any]] = []
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str):
                        t = item
                        if t in TASK_REGISTRY:
                            parsed_nodes.append({
                                "task_type": t,
                                "duration": 0  # default to no override
                            })
                    elif isinstance(item, dict):
                        # Prefer explicit task_type; treat "type" as alias
                        t = item.get("task_type") or item.get("type") or item.get("phase")
                        if t in TASK_REGISTRY:
                            if item.get("enabled") is False:
                                continue
                            rawd = item.get("duration")
                            dur = 0 if (rawd is None or rawd == 0 or rawd == "0") else _normalize_duration(t, rawd)
                            parsed_nodes.append({
                                "task_type": t,
                                "duration": dur,
                                "phase": item.get("phase"),
                                "description": item.get("description"),
                            })
            if parsed_nodes:
                return parsed_nodes
    except Exception:
        # If parsing fails, fall back to defaults
        pass

    # 3) Fallback to global TASK_SEQUENCE using registry defaults
    fallback: list[dict[str, Any]] = []
    for t in TASK_SEQUENCE:
        if t in TASK_REGISTRY:
            fallback.append({
                "task_type": t,
                "duration": 0  # default to no override
            })
    return fallback


def _make_phase_context(workshop: Workshop, next_index: int, next_task_type: str) -> str:
    action_plan_json = workshop.task_sequence or '[]'
    try:
        action_plan_list = json.loads(action_plan_json)
        phase_data = action_plan_list[next_index] if 0 <= next_index < len(action_plan_list) else {}
        return f"Phase: {phase_data.get('phase', 'N/A')}\nDescription: {phase_data.get('description', 'N/A')}"
    except Exception:
        return f"Task Type: {next_task_type}"


def _emit_for_task_type(room: str, task_type: str, payload: dict):
    # Normalize legacy warm-up naming to canonical type
    canonical_type = "warm-up" if task_type in {"warm-up", "warm_up", "introduction"} else task_type
    if canonical_type != task_type and current_app:
        try:
            current_app.logger.info(
                "[WarmUp] Normalized legacy task type '%s' to '%s' for room %s",
                task_type,
                canonical_type,
                room,
            )
        except Exception:
            pass

    # Use registry for event; fallback to legacy mapping
    meta = TASK_REGISTRY.get(canonical_type) or {}
    event = meta.get("event")
    if not event:
        if canonical_type == "brainstorming":
            event = "task_ready"
        elif canonical_type == "warm-up":
            event = "warm_up_start"
        elif canonical_type == "clustering_voting":
            event = "clusters_ready"
        elif canonical_type == "results_feasibility":
            event = "feasibility_ready"
        elif canonical_type == "discussion":
            event = "discussion_ready"
        elif canonical_type == "summary":
            event = "summary_ready"
        else:
            event = "task_ready"  # Default fallback
    
    # Actually emit the event
    socketio.emit(event, payload, to=room)
    if current_app:
        try:
            current_app.logger.info(f"[Workshop] Emitted {event} to {room} for task type {canonical_type}")
        except Exception:
            pass
def _find_latest_task_id(workshop_id: int, task_type: str) -> int | None:
    """Return the most recent task id of a given type for this workshop, if any.
    Prefers tasks that actually started; falls back to newest by id.
    """
    try:
        q = (
            BrainstormTask.query
            .filter_by(workshop_id=workshop_id, task_type=task_type)
            .order_by(BrainstormTask.started_at.desc().nullslast(), BrainstormTask.id.desc())
        )
        obj = q.first()
        return obj.id if obj else None
    except Exception:
        return None


def advance_to_next_task(workshop_id: int):
    """Advance to the next task in the sequence and broadcast. Returns (ok, payload_or_error).
    Safe to call from background threads.

    Behavior improvements:
    - If a dependent phase cannot start (e.g., clustering with no ideas, feasibility with no votes),
      gracefully skip to the next actionable phase instead of failing with 400.
    """
    logger = current_app.logger if current_app else None
    try:
        workshop = db.session.get(Workshop, workshop_id)
        if not workshop:
            return False, "Workshop not found"
        if workshop.status != "inprogress":
            return False, f"Workshop status is {workshop.status}"

        # Mark previous task completed if running
        if workshop.current_task_id:
            previous_task = db.session.get(BrainstormTask, workshop.current_task_id)
            if previous_task and previous_task.status == 'running':
                previous_task.status = 'completed'
                previous_task.ended_at = datetime.utcnow()
                prev_type = (previous_task.task_type or "").replace("_", "-").strip().lower()
                if prev_type == "warm-up":
                    _emit_warm_up_completed(workshop, previous_task)

        plan_nodes = _get_plan_nodes(workshop)
        task_sequence = [n["task_type"] for n in plan_nodes]
        if not task_sequence:
            return False, "No tasks in the action plan."

        current_index = workshop.current_task_index if workshop.current_task_index is not None else -1
        cand_index = current_index + 1

        # Helper to try generate payload for a given type/index; returns (ok, payload_or_err, skippable)
        def _try_generate(idx: int, ttype: str) -> tuple[bool, dict[str, Any] | str, bool]:
            ctx = _make_phase_context(workshop, idx, ttype)
            res: dict[str, Any] | tuple[object, int] | None
            if ttype in ("warm-up", "warm_up", "introduction"):
                res = get_warm_up_payload(workshop_id, ctx)
            elif ttype == "brainstorming":
                res = get_brainstorming_task_payload(workshop_id, ctx)
            elif ttype == "clustering_voting":
                # Prefer latest brainstorming task as base; else fall back to current
                base_id = _find_latest_task_id(workshop.id, "brainstorming") or workshop.current_task_id
                if not base_id:
                    return False, "Cannot start clustering without a prior Brainstorming phase.", True
                res = get_clustering_voting_payload(workshop_id, base_id, ctx)
            elif ttype == "results_feasibility":
                # Prefer latest clustering/voting task as base; else fall back to current
                base_id = _find_latest_task_id(workshop.id, "clustering_voting") or workshop.current_task_id
                if not base_id:
                    return False, "Cannot start feasibility without a prior Clustering/Voting phase.", True
                res = get_feasibility_payload(workshop_id, base_id, ctx)
            elif ttype == "results_prioritization":
                base_id = _find_latest_task_id(workshop.id, "clustering_voting")
                if not base_id:
                    return False, "Cannot start prioritization without a prior Clustering/Voting phase.", True
                res = get_prioritization_payload(workshop_id, base_id, ctx)
            elif ttype == "results_action_plan":
                res = get_action_plan_payload(workshop_id, ctx)
            elif ttype == "discussion":
                res = get_discussion_payload(workshop_id, ctx)
            elif ttype == "summary":
                res = get_summary_payload(workshop_id, ctx)
            elif ttype == "meeting":
                res = get_meeting_payload(workshop_id, ctx)
            elif ttype == "presentation":
                res = get_presentation_payload(workshop_id, ctx)
            elif ttype == "framing":
                res = get_framing_payload(workshop_id, ctx)
            elif ttype == "speech":
                res = get_speech_payload(workshop_id, ctx)
            elif ttype == "vote_generic":
                res = get_vote_generic_payload(workshop_id, ctx)
            else:
                return False, f"Unsupported task type: {ttype}", True

            if isinstance(res, tuple):
                err_msg = str(res[0]) if res else ""
                # Treat known data-dependent errors as skippable
                msg = err_msg
                if (
                    "No ideas" in msg
                    or "No voted clusters" in msg
                    or "Cannot start clustering" in msg
                    or "Cannot start feasibility" in msg
                ):
                    return False, msg, True
                return False, msg, False
            if not isinstance(res, dict):
                return False, "Internal error generating task payload.", False
            return True, res, False

        # Iterate until we find a task we can start, or exhaust the plan
        chosen_index: int | None = None
        chosen_task_type: str | None = None
        task_payload: dict[str, Any] | None = None
        while cand_index < len(task_sequence):
            next_task_type = task_sequence[cand_index]
            ok_gen, res, skippable = _try_generate(cand_index, next_task_type)
            if ok_gen:
                if not isinstance(res, dict):
                    return False, "Internal error generating task payload."
                chosen_index = cand_index
                chosen_task_type = next_task_type
                task_payload = res
                break
            if skippable:
                if logger:
                    logger.info(f"[Auto] Skipping phase '{next_task_type}' due to unmet prerequisites: {res}")
                cand_index += 1
                continue
            # Non-skippable error
            return False, res

        if task_payload is None:
            return False, "No more tasks in the action plan."

        # Ensure expected types
        if not isinstance(task_payload, dict):
            return False, "Internal error generating task payload."
        if not isinstance(chosen_index, int):
            return False, "Internal error choosing task index."
        if not isinstance(chosen_task_type, str):
            chosen_task_type = str(task_payload.get("task_type") or task_sequence[chosen_index])

        # Include chosen index in payload for client-side flow highlighting
        try:
            if isinstance(task_payload, dict):
                task_payload["task_index"] = chosen_index
        except Exception:
            pass

        # Apply organizer-defined duration override into payload prior to validation
        desired_duration = None
        try:
            if 0 <= chosen_index < len(plan_nodes):
                node = plan_nodes[chosen_index] if isinstance(plan_nodes, list) else None
                if isinstance(node, dict):
                    dd = node.get("duration")
                    # Only treat positive values as explicit overrides; 0 means no override
                    if dd is not None:
                        desired_duration = int(dd)
        except Exception:
            desired_duration = None
        if desired_duration is not None and desired_duration > 0:
            try:
                task_payload["task_duration"] = int(desired_duration)
            except Exception:
                pass

        # Minimal schema validation
        if not validate_payload(chosen_task_type, task_payload):
            db.session.rollback()
            return False, f"Invalid payload for task type: {chosen_task_type}"

        # Optional: duration override from config (subordinate to organizer-defined plan)
        try:
            override = (current_app.config.get('DEBUG_OVERRIDE_TASK_DURATION') if current_app else None)
            # Only apply if no explicit duration set by organizer
            if override and isinstance(override, (int, str)):
                node = plan_nodes[chosen_index] if (isinstance(chosen_index, int) and 0 <= chosen_index < len(plan_nodes)) else None
                no_explicit = (isinstance(node, dict) and not node.get("duration"))
                if no_explicit:
                    d = int(override)
                    try:
                        task_payload['task_duration'] = d
                    except Exception:
                        pass
        except Exception:
            pass

        new_task_id = task_payload.get('task_id') if isinstance(task_payload, dict) else None
        if not new_task_id:
            return False, "Internal error: Task ID missing after generation."

        new_task = db.session.get(BrainstormTask, new_task_id)
        if not new_task:
            return False, "Internal error: Failed to retrieve new task."

        # Update workshop + task (also enforce DB duration to match payload/plan)
        now_utc = datetime.utcnow()
        workshop.current_task_id = new_task_id
        workshop.current_task_index = chosen_index
        workshop.timer_start_time = now_utc
        workshop.timer_paused_at = None
        workshop.timer_elapsed_before_pause = 0
        new_task.status = 'running'
        new_task.started_at = workshop.timer_start_time
        workshop.phase_started_at = now_utc
        try:
            enforced_raw = task_payload.get('task_duration') if isinstance(task_payload, dict) else None
            if enforced_raw is not None:
                enforced = int(enforced_raw)
                if enforced and enforced != new_task.duration:
                    new_task.duration = enforced
        except Exception:
            # If anything goes wrong, leave existing duration
            pass
        db.session.commit()

        # Persist facilitator narration as a real Transcript row (one-time per task)
        try:
            _persist_facilitator_transcript(workshop, new_task)
        except Exception as _e:
            if logger:
                logger.warning(f"Facilitator transcript persistence skipped: {_e}")

        # Broadcast to room
        room = f"workshop_room_{workshop_id}"
        task_type_str = task_payload.get("task_type") if isinstance(task_payload, dict) else None
        task_type_str = task_type_str or "brainstorming"
        _emit_for_task_type(room, task_type_str, task_payload if isinstance(task_payload, dict) else {})
        # Emit initial timer sync
        emit_timer_sync(
            room,
            {
                "task_id": new_task_id,
                "remaining_seconds": new_task.duration,
                "is_paused": False,
            },
            workshop_id=workshop_id,
        )
        emit_assistant_state(
            workshop_id,
            include_sidebar=True,
            include_phase_snapshot=True,
        )

        if logger:
            try:
                tt = task_payload.get('task_type') if isinstance(task_payload, dict) else None
            except Exception:
                tt = None
            logger.info(f"[Auto] Workshop {workshop_id} advanced to task {new_task_id} (Index: {chosen_index}, Type: {tt})")
        return True, task_payload
    except Exception as e:
        db.session.rollback()
        if logger:
            logger.error(f"Error auto-advancing workshop {workshop_id}: {e}", exc_info=True)
        return False, "Server error during auto-advance"


def go_to_task(workshop_id: int, target_index: int):
    """Jump to a specific task index in the plan and broadcast it.
    Returns (ok, payload_or_error).

    Behavior:
    - Completes the currently running task (if any) before switching.
    - Generates payload for the requested index (skipping is not applied here; caller chooses index).
    - Emits the correct event for the selected task type and an initial timer_sync.
    """
    logger = current_app.logger if current_app else None
    try:
        workshop = db.session.get(Workshop, workshop_id)
        if not workshop:
            return False, "Workshop not found"
        if workshop.status not in ("inprogress", "paused"):
            return False, f"Workshop status is {workshop.status}"

        # Mark previous task completed if running
        if workshop.current_task_id:
            previous_task = db.session.get(BrainstormTask, workshop.current_task_id)
            if previous_task and previous_task.status == 'running':
                previous_task.status = 'completed'
                previous_task.ended_at = datetime.utcnow()

        plan_nodes = _get_plan_nodes(workshop)
        task_sequence = [n["task_type"] for n in plan_nodes]
        if not task_sequence:
            return False, "No tasks in the action plan."

        if not isinstance(target_index, int) or not (0 <= target_index < len(task_sequence)):
            return False, "Invalid target index."

        ttype = task_sequence[target_index]

        # Helper to generate payload for a single index/type
        def _try_generate(idx: int, t: str):
            ctx = _make_phase_context(workshop, idx, t)
            res: dict[str, Any] | tuple[object, int] | None
            if t in ("warm-up", "warm_up", "introduction"):
                res = get_warm_up_payload(workshop_id, ctx)
            elif t == "brainstorming":
                res = get_brainstorming_task_payload(workshop_id, ctx)
            elif t == "clustering_voting":
                # For direct navigation, choose the latest brainstorming task as dependency
                base_id = _find_latest_task_id(workshop.id, "brainstorming")
                if not base_id:
                    return False, "Cannot start clustering without a prior Brainstorming phase.", False
                res = get_clustering_voting_payload(workshop_id, base_id, ctx)
            elif t == "results_feasibility":
                # For direct navigation, use the latest Clustering/Voting task as dependency
                base_id = _find_latest_task_id(workshop.id, "clustering_voting")
                if not base_id:
                    return False, "Cannot start feasibility without a prior Clustering/Voting phase.", False
                res = get_feasibility_payload(workshop_id, base_id, ctx)
            elif t == "results_prioritization":
                base_id = _find_latest_task_id(workshop.id, "clustering_voting")
                if not base_id:
                    return False, "Cannot start prioritization without a prior Clustering/Voting phase.", False
                res = get_prioritization_payload(workshop_id, base_id, ctx)
            elif t == "results_action_plan":
                res = get_action_plan_payload(workshop_id, ctx)
            elif t == "discussion":
                res = get_discussion_payload(workshop_id, ctx)
            elif t == "summary":
                res = get_summary_payload(workshop_id, ctx)
            elif t == "meeting":
                res = get_meeting_payload(workshop_id, ctx)
            elif t == "presentation":
                res = get_presentation_payload(workshop_id, ctx)
            elif t == "framing":
                res = get_framing_payload(workshop_id, ctx)
            elif t == "speech":
                res = get_speech_payload(workshop_id, ctx)
            elif t == "vote_generic":
                res = get_vote_generic_payload(workshop_id, ctx)
            else:
                return False, f"Unsupported task type: {t}", True

            if isinstance(res, tuple):
                msg = str(res[0]) if res else ""
                return False, msg, False
            if not isinstance(res, dict):
                return False, "Internal error generating task payload.", False
            return True, res, False

        ok_gen, payload, _ = _try_generate(target_index, ttype)
        if not ok_gen:
            return False, payload

        # Include target index in payload for client-side flow highlighting
        try:
            if isinstance(payload, dict):
                payload["task_index"] = target_index
        except Exception:
            pass

        # Apply organizer-defined duration override into payload prior to validation
        desired_duration = None
        try:
            node = plan_nodes[target_index] if 0 <= target_index < len(plan_nodes) else None
            if isinstance(node, dict):
                dd = node.get("duration")
                if dd is not None:
                    desired_duration = int(dd)
        except Exception:
            desired_duration = None
        if desired_duration is not None and isinstance(payload, dict):
            try:
                payload["task_duration"] = int(desired_duration)
            except Exception:
                pass

        # Validate
        if not isinstance(ttype, str) or not isinstance(payload, dict) or not validate_payload(ttype, payload):
            db.session.rollback()
            return False, f"Invalid payload for task type: {ttype}"

        new_task_id = payload.get('task_id') if isinstance(payload, dict) else None
        if not new_task_id:
            return False, "Internal error: Task ID missing after generation."

        new_task = db.session.get(BrainstormTask, new_task_id)
        if not new_task:
            return False, "Internal error: Failed to retrieve new task."

        # Update workshop + task (also enforce DB duration to match payload/plan)
        now_utc = datetime.utcnow()
        workshop.current_task_id = new_task_id
        workshop.current_task_index = target_index
        workshop.timer_start_time = now_utc
        workshop.timer_paused_at = None
        workshop.timer_elapsed_before_pause = 0
        new_task.status = 'running'
        new_task.started_at = workshop.timer_start_time
        workshop.phase_started_at = now_utc
        try:
            enforced_raw = payload.get('task_duration') if isinstance(payload, dict) else None
            if enforced_raw is not None:
                enforced = int(enforced_raw)
                if enforced and enforced != new_task.duration:
                    new_task.duration = enforced
        except Exception:
            pass
        db.session.commit()

        # Persist facilitator narration as a real Transcript row (one-time per task)
        try:
            _persist_facilitator_transcript(workshop, new_task)
        except Exception as _e:
            if logger:
                logger.warning(f"Facilitator transcript persistence skipped: {_e}")

        # Broadcast to room
        room = f"workshop_room_{workshop_id}"
        task_type_str = payload.get("task_type") if isinstance(payload, dict) else None
        task_type_str = task_type_str or ttype
        _emit_for_task_type(room, task_type_str, payload if isinstance(payload, dict) else {})
        # Emit initial timer sync
        emit_timer_sync(
            room,
            {
                "task_id": new_task_id,
                "remaining_seconds": new_task.duration,
                "is_paused": False,
            },
            workshop_id=workshop_id,
        )
        emit_assistant_state(
            workshop_id,
            include_sidebar=True,
            include_phase_snapshot=True,
        )

        if logger:
            logger.info(f"[Navigate] Workshop {workshop_id} jumped to index {target_index} (Task {new_task_id}, Type: {task_type_str})")
        return True, payload
    except Exception as e:
        db.session.rollback()
        if logger:
            logger.error(f"Error navigating workshop {workshop_id} to index {target_index}: {e}", exc_info=True)
        return False, "Server error during task navigation"

def _persist_facilitator_transcript(workshop: Workshop, task: BrainstormTask) -> None:
    """Persist the task's facilitator narration (tts_script/narration) as a Transcript row.

    - Avoid duplicates by checking for an existing identical Transcript for this workshop
      from the facilitator user.
    - Use task.started_at as created timestamp if available for chronological ordering.
    """
    if not task or not isinstance(task, BrainstormTask):
        return
    text = None
    payload = None
    # Prefer structured payload_json; fall back to prompt if needed
    if task.payload_json:
        try:
            payload = json.loads(task.payload_json)
        except Exception:
            payload = None
    else:
        # Some generators may have placed data in prompt
        if task.prompt:
            try:
                payload = json.loads(task.prompt)
            except Exception:
                payload = None
    if isinstance(payload, dict):
        text = (
            payload.get("tts_script")
            or payload.get("narration")
            or payload.get("summary_tts_script")
            or None
        )
    if not (isinstance(text, str) and text.strip()):
        return
    fac = get_or_create_facilitator_user()
    # De-dup: if an identical facilitator transcript already exists for this workshop, skip
    exists = (
        db.session.query(Transcript)
        .filter(
            Transcript.workshop_id == workshop.id,
            Transcript.user_id == fac.user_id,
            Transcript.raw_stt_transcript == text.strip(),
        )
        .order_by(Transcript.created_timestamp.desc())
        .first()
    )
    if exists:
        return
    tr = Transcript()
    tr.workshop_id = workshop.id
    tr.user_id = fac.user_id
    try:
        tr.task_id = int(task.id)
    except Exception:
        pass
    try:
        tr.entry_type = 'facilitator'
    except Exception:
        # Column may not exist yet in legacy DB; ignore assignment error
        pass
    tr.raw_stt_transcript = text.strip()
    tr.processed_transcript = text.strip()
    tr.language = "en-US"
    # Stamp ordering time close to task start if available
    tr.start_timestamp = None
    tr.end_timestamp = None
    # created_timestamp defaults to utcnow; optionally align to task.started_at
    if task.started_at:
        try:
            tr.created_timestamp = task.started_at
        except Exception:
            pass
    db.session.add(tr)
    db.session.commit()
