# app/workshop/routes.py
from __future__ import annotations

import os, markdown, json, re, html, copy, shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple, cast
from uuid import uuid4

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort, current_app
from flask.typing import ResponseReturnValue
from flask_login import login_required, current_user
from sqlalchemy import or_, func
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.exc import IntegrityError

from app.extensions import db, socketio
from app.models import (
    Workshop,
    Workspace,
    WorkspaceMember,
    WorkshopParticipant,
    Document,
    WorkshopDocument,
    User,
    BrainstormTask,
    BrainstormIdea,
    IdeaCluster,
    IdeaVote,
    ChatMessage,
    WorkshopPlanItem,
    ActionItem,
    WorkshopAgenda,
    Transcript,
    User,
)
from app.config import TASK_SEQUENCE, Config
from app.service.routes.agenda import generate_agenda_text
from app.service.routes.rules import generate_rules_text
from app.service.routes.icebreaker import generate_icebreaker_text
from app.service.routes.tip import generate_tip_text
from app.service.routes.actions import import_action_items
from app.service.agenda_pipeline import run_agenda_pipeline, AgendaGenerationError
from app.document.service.pipeline import run_pipeline
from app.workshop.advance import advance_to_next_task
from app.workshop.advance import go_to_task
from app.service.routes.speech import build_speech_preview
from app.service.routes.framing import build_framing_preview
from app.service.routes.presentation import rebuild_presentation_artifacts
from app.service.routes import warm_up as warm_up_service
from app.sockets import (
    emit_workshop_stopped,
    emit_workshop_paused,
    emit_workshop_resumed,
    _room_presence,
    _sid_registry,
    _broadcast_participant_list,
)
from markupsafe import escape
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

PlanNode = Mapping[str, Any]


def _safe_int(value: Any, default: int = 0) -> int:
    """Best-effort conversion to int with graceful fallback."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except (ValueError, TypeError):
            return default
    return default


def _safe_float(value: Any) -> float | None:
    """Convert to float when possible; return None on failure."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except (ValueError, TypeError):
            return None
    return None

# --- Agenda generation helpers ------------------------------------------------


def _agenda_flash_for_failure(error: AgendaGenerationError) -> Tuple[Optional[str], Optional[str]]:
    """Return (warning, notice) flash messages for agenda generation failures."""

    code = getattr(error, "code", None)
    if code == "bedrock_not_configured":
        return (
            None,
            "Agenda AI generation is disabled because AWS Bedrock credentials aren't configured. "
            "We saved your draft exactly as typed.",
        )
    if code == "llm_initialization_failed":
        return (
            "Agenda AI libraries are missing or misconfigured. Install 'langchain-aws' and verify AWS credentials "
            "to enable auto-structuring. We saved your draft exactly as typed.",
            None,
        )
    if code == "validation_failed":
        return (
            "The AI response didn't match the expected agenda format. We saved your draft so you can adjust and try again.",
            None,
        )
    return (
        "We saved your agenda draft, but the AI assistant is unavailable right now.",
        None,
    )

# Import send_email utility from auth routes
from app.auth.routes import send_email
from app.service.routes.moderator import clear_workshop_tracking, check_and_nudge

# Define blueprint
workshop_bp = Blueprint('workshop_bp', __name__, template_folder="templates")

# --- Internal helpers ---------------------------------------------------------


def _prepare_workshop_for_delete(workshop: Workshop) -> None:
    """Clear circular references so ORM cascades can delete cleanly."""
    tasks_rel = getattr(workshop, "tasks", None)
    if tasks_rel is None:
        return
    if hasattr(tasks_rel, "all"):
        tasks = list(tasks_rel.all())
    else:
        tasks = list(tasks_rel)

    for task in tasks:
        clusters_rel = getattr(task, "clusters", None)
        if clusters_rel is None:
            clusters: list[IdeaCluster] = []
        elif hasattr(clusters_rel, "all"):
            clusters = list(clusters_rel.all())
        else:
            clusters = list(clusters_rel)
        for cluster in clusters:
            cluster.representative_idea_id = None

        ideas_rel = getattr(task, "ideas", None)
        if ideas_rel is None:
            ideas: list[BrainstormIdea] = []
        elif hasattr(ideas_rel, "all"):
            ideas = list(ideas_rel.all())
        else:
            ideas = list(ideas_rel)
        for idea in ideas:
            idea.cluster_id = None
            if getattr(idea, "duplicate_of_id", None) is not None:
                idea.duplicate_of_id = None

# --- Lobby TTS helpers -------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_BREAK_RE = re.compile(r"</\s*(p|div|li|ul|ol|section|article|h[1-6])\s*>", re.IGNORECASE)


def _html_to_plaintext(html_text: str | None) -> str:
    if not html_text:
        return ""
    text = _BR_TAG_RE.sub("\n", html_text)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _markdown_to_plaintext(markdown_text: str | None) -> str:
    if not markdown_text:
        return ""
    return _html_to_plaintext(markdown.markdown(markdown_text))


def _agenda_items_to_plain(items) -> str:
    if not items:
        return ""

    def _position(it):
        if isinstance(it, dict):
            return it.get("position") or 0
        return getattr(it, "position", 0) or 0

    ordered = sorted(list(items), key=_position)
    segments: list[str] = []
    for idx, item in enumerate(ordered, start=1):
        if isinstance(item, dict):
            title = (item.get("activity") or item.get("activity_title") or item.get("title") or item.get("description") or "").strip()
            description = (item.get("description") or item.get("activity_description") or "").strip()
            duration = item.get("estimated_duration")
            time_slot = item.get("time_slot")
        else:
            title = (getattr(item, "activity_title", "") or "").strip()
            description = (getattr(item, "activity_description", "") or "").strip()
            duration = getattr(item, "estimated_duration", None)
            time_slot = getattr(item, "time_slot", None)

        if not title and description:
            title = description.split(". ")[0].strip()

        segment_parts = [f"{idx}. {title or 'Agenda item'}"]
        if duration:
            segment_parts.append(f"({duration} minutes)")
        elif time_slot:
            segment_parts.append(f"({time_slot})")

        segment = " ".join([part for part in segment_parts if part])
        if description and description.lower() != (title or "").lower():
            segment = f"{segment}: {description}"

        segments.append(segment.strip())

    if not segments:
        return ""

    return "Agenda preview. " + " ".join(segments)


def _compute_tts_for_lobby(kind: str, *, raw: str | None = None, html_text: str | None = None, items=None) -> str:
    kind = (kind or "").lower()
    if kind == "agenda":
        source_items = list(items or [])
        if not source_items and isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    source_items = parsed.get("agenda") or []
                elif isinstance(parsed, list):
                    source_items = parsed
            except Exception:
                source_items = []
        text = _agenda_items_to_plain(source_items)
        if not text and raw:
            text = _markdown_to_plaintext(raw)
        if not text and html_text:
            text = _html_to_plaintext(html_text)
        return text.strip()

    text = _markdown_to_plaintext(raw) if isinstance(raw, str) else ""
    if not text and html_text:
        text = _html_to_plaintext(html_text)
    return text.strip()

# ----------------- Plan (sequence + per-step durations) API helpers -----------------
def _serialize_default_plan():
    from app.tasks.registry import TASK_REGISTRY
    nodes = []
    for t in TASK_SEQUENCE:
        if t in TASK_REGISTRY:
            nodes.append({
                "task_type": t,
                "duration": 0,  # 0 = no override (use LLM duration by default)
                "phase": None,
                "description": None,
                "enabled": True,
            })
    return nodes


def _build_default_plan_for_workshop(ws: Workshop) -> list[dict]:
    """Construct the default plan with config_json defaults, matching requested behavior.

        Rules:
        - Brainstorm: framing -> warm-up -> brainstorming -> clustering_voting -> results_feasibility -> results_prioritization -> discussion -> results_action_plan -> summary
            (Facilitators can optionally add meeting or vote_generic steps from the UI if they want additional segments.)
    - Meeting: meeting -> discussion -> summary
    - Presentation: warm-up -> presentation(presenter=organizer, doc=first linked if any) -> discussion -> summary
    - Custom/other: fall back to TASK_SEQUENCE with no special configs
    """
    from app.tasks.registry import TASK_REGISTRY
    def _node(t: str, dur: int | None = None, desc: str | None = None, cfg: dict | None = None):
        return {
            'task_type': t,
            'duration': int(dur) if dur is not None else 0,  # 0 = no override
            'phase': None,
            'description': desc,
            'config_json': (cfg if isinstance(cfg, dict) and len(cfg) > 0 else None),
            'enabled': True,
        }

    # Normalize workshop type
    try:
        wtype = (getattr(ws, 'type', None) or 'brainstorm').strip().lower()
    except Exception:
        wtype = 'brainstorm'

    # Organizer id for defaults (presentation)
    organizer_id = getattr(ws, 'created_by_id', None)

    # First linked document (if any) for presentation default
    first_doc_id = None
    try:
        # Workshop.linked_documents is likely a dynamic relationship; use query
        link = WorkshopDocument.query.filter_by(workshop_id=ws.id).order_by(WorkshopDocument.id.asc()).first()
        if link:
            first_doc_id = getattr(link, 'document_id', None)
    except Exception:
        first_doc_id = None

    if wtype in ('brainstorm', 'brainstorming'):
        # Requested default order: framing, warm-up, brainstorming, clustering_voting,
        # results_feasibility, results_prioritization, discussion, results_action_plan, summary
        return [
            _node('framing'),
            _node('warm-up'),
            _node('brainstorming'),
            _node('clustering_voting'),
            _node('results_feasibility'),
            _node('results_prioritization'),
            _node('discussion'),
            _node('results_action_plan'),
            _node('summary'),
        ]
    if wtype == 'meeting':
        return [
            _node('meeting'),
            _node('discussion'),
            _node('summary'),
        ]
    if wtype == 'presentation':
        pres_cfg = {}
        if organizer_id is not None:
            pres_cfg['presenter_user_id'] = organizer_id
        if first_doc_id is not None:
            pres_cfg['document_id'] = first_doc_id
        return [
            _node('warm-up'),
            _node('presentation', cfg=pres_cfg),
            _node('discussion'),
            _node('summary'),
        ]

    # Fallback: TASK_SEQUENCE ordered basics
    plan = []
    from app.config import TASK_SEQUENCE
    for t in TASK_SEQUENCE:
        if t in TASK_REGISTRY:
            plan.append(_node(t))
    return plan


def _seed_plan_for_workshop(ws: Workshop):
    """Persist initial DB-backed plan items using the default plan builder."""
    plan = _build_default_plan_for_workshop(ws)
    # Persist into WorkshopPlanItem
    WorkshopPlanItem.query.filter_by(workshop_id=ws.id).delete()
    db.session.flush()
    nodes_for_json = []
    for idx, node in enumerate(plan):
        it = WorkshopPlanItem()
        it.workshop_id = ws.id
        it.order_index = idx
        it.task_type = node['task_type']
        it.duration = int(node.get('duration') or 0)
        it.phase = node.get('phase')
        it.description = node.get('description')
        cfg = node.get('config_json')
        if cfg is not None:
            try:
                it.config_json = json.dumps(cfg)
            except Exception:
                it.config_json = None
        it.enabled = True
        db.session.add(it)
        # Mirror simple JSON (keep config_json embedded)
        safe = dict(node)
        nodes_for_json.append(safe)
    try:
        ws.task_sequence = json.dumps(nodes_for_json)
    except Exception:
        ws.task_sequence = None
    db.session.flush()


def _validate_plan(payload):
    """Validate posted plan array. Returns (ok, normalized_nodes | error_msg)."""
    from app.tasks.registry import TASK_REGISTRY
    # Define simple dependency rules using IO model
    # A step is allowed if all its declared inputs exist in the outputs of some prior step in the plan
    # Special cases:
    # - 'discussion' allowed anywhere (no required inputs)
    # - 'summary' typically last, but we won't enforce last-only to keep flexibility; it has no hard inputs
    # Allow unlimited duplicates of these types
    DUPLICATE_ALLOWED = {"discussion", "brainstorming", "clustering_voting", "results_feasibility"}
    if not isinstance(payload, list):
        return False, "Plan must be a list"
    norm = []
    produced: list[str] = []
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            return False, f"Plan item {i} must be an object"
        t = item.get("task_type") or item.get("type")
        if t not in TASK_REGISTRY:
            return False, f"Unknown task_type at index {i}"
        enabled = item.get("enabled", True)
        if not enabled:
            # Skip disabled nodes entirely
            continue
        # Duration in seconds: 0 or missing means no override; positive values are clamped.
        rawd = item.get("duration")
        try:
            d = int(rawd) if rawd is not None else 0
        except Exception:
            return False, f"Invalid duration at index {i}"
        if d < 0:
            return False, f"Invalid duration at index {i}"
        if d > 0:
            d = max(30, min(7200, d))
        # IO dependency check (except for discussion which is free-form)
        # Dependency rules with stage-awareness for vote_generic
        req_inputs = (TASK_REGISTRY.get(t) or {}).get("inputs", []) or []
        if t == "vote_generic":
            # If configured as stage 1 (clusters), require clusters from a prior step.
            cfg = item.get("config_json") if isinstance(item, dict) else None
            stage = None
            if isinstance(cfg, dict):
                stage = cfg.get("stage")
            # Default stage is clusters if unspecified
            stage = stage or "clusters"
            if stage == "clusters" or stage == "ideas_from_top_cluster":
                if "clusters" not in produced:
                    return False, f"'vote_generic' (stage={stage}) at position {i+1} requires prior output 'clusters'. Move it after Clustering."
            elif stage == "manual":
                # No hard prerequisites
                pass
            else:
                # For any other stage (e.g., direct ideas voting), require either ideas or clusters produced earlier
                if ("ideas" not in produced) and ("clusters" not in produced):
                    return False, f"'vote_generic' (stage={stage}) at position {i+1} requires prior output 'ideas' or 'clusters'."
        elif t not in ("discussion",):
            for inp in req_inputs:
                # Support optional inputs marked with a trailing '?'
                if isinstance(inp, str) and inp.endswith('?'):
                    base = inp[:-1]
                    # Optional: no enforcement
                    continue
                if inp not in produced:
                    return False, f"'{t}' at position {i+1} requires prior output '{inp}'. Move it after its producing phase."

        # Record outputs from this step (for subsequent steps)
        outs = (TASK_REGISTRY.get(t) or {}).get("outputs", []) or []
        for o in outs:
            if o not in produced:
                produced.append(o)

        node_norm = {
            "task_type": t,
            "duration": d,
            "phase": item.get("phase"),
            "description": item.get("description"),
            "enabled": True,
        }
        # Pass through config_json if provided (used by presentation/speech/vote_generic, etc.)
        if "config_json" in item:
            node_norm["config_json"] = item.get("config_json")
        norm.append(node_norm)
    if not norm:
        return False, "Plan cannot be empty"
    return True, norm

###################################
# Workshops: List / Create / View / Edit / Delete
###################################

@workshop_bp.route("/workshops")
@login_required
def list_workshops() -> ResponseReturnValue:
    """List workshops the user can access (by workspace membership, participation, or ownership)."""
    try:
        user = cast(User, current_user)
        # Active workspaces the user belongs to
        memberships = WorkspaceMember.query.filter_by(
            user_id=user.user_id, status="active"
        ).all()
        active_workspace_ids: list[int] = [int(wm.workspace_id) for wm in memberships]

        # Subquery (as a selectable): workshops where the user is a participant
        participant_workshop_ids_query = (
            db.session.query(WorkshopParticipant.workshop_id)
            .filter(WorkshopParticipant.user_id == user.user_id)
        )

        filters: list[Any] = []
        if active_workspace_ids:
            filters.append(Workshop.workspace_id.in_(active_workspace_ids))
        # Always include ones the user created or participates in
        filters.append(Workshop.created_by_id == user.user_id)
        filters.append(Workshop.id.in_(participant_workshop_ids_query))

        workshops = (
            db.session.query(Workshop)
            .filter(or_(*filters))
            .order_by(Workshop.date_time.desc())
        ).all()
    except Exception as e:
        current_app.logger.error(f"Error loading workshops list: {e}")
        workshops = []

    return render_template("workshop_list.html", workshops=workshops)


@workshop_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_workshop_general() -> ResponseReturnValue:
    """Create a workshop with workspace selector (general entry)."""
    user = cast(User, current_user)
    if request.method == "POST":
        # Validate inputs
        workspace_id = request.form.get("workspace_id", type=int)
        title = (request.form.get("title") or "").strip()
        objective = (request.form.get("objective") or "").strip()
        date_time_str = (request.form.get("date_time") or "").strip()
        duration = request.form.get("duration", type=int)
        agenda = (request.form.get("agenda") or "").strip()
        auto_generate_agenda = (request.form.get("auto_generate_agenda") == "on")
        linked_document_ids = [value for value in request.form.getlist("linked_document_ids") if value]
        upload_files = request.files.getlist("reference_uploads")
        uploaded_doc_ids: list[int] = []
        upload_warnings: list[str] = []

        form_data = type("obj", (), dict(
            workspace_id=str(workspace_id) if workspace_id else "",
            title=title,
            objective=objective,
            date_time=date_time_str,
            duration=duration if duration is not None else "",
            agenda=agenda,
            auto_generate_agenda=auto_generate_agenda,
            linked_document_ids=linked_document_ids,
        ))

        # Basic checks
        if not workspace_id:
            flash("Workspace is required.", "danger")
            workspaces = get_user_active_workspaces(user.user_id)
            workspace_documents = get_reference_documents_for_user(user.user_id)
            return render_template(
                "workshop_create.html",
                workspaces=workspaces,
                show_workspace_select=True,
                workspace=None,
                form_data=form_data,
                workspace_documents=workspace_documents,
            )
        if not title or not date_time_str:
            flash("Title and date/time are required.", "danger")
            workspaces = get_user_active_workspaces(user.user_id)
            workspace_documents = get_reference_documents_for_user(user.user_id, workspace_id)
            return render_template(
                "workshop_create.html",
                workspaces=workspaces,
                show_workspace_select=True,
                workspace=None,
                form_data=form_data,
                workspace_documents=workspace_documents,
            )

        # Verify workspace membership
        membership = WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=user.user_id, status='active').first()
        if not membership:
            flash("You must be an active member of the workspace.", "danger")
            workspaces = get_user_active_workspaces(user.user_id)
            workspace_documents = get_reference_documents_for_user(user.user_id, workspace_id)
            return render_template(
                "workshop_create.html",
                workspaces=workspaces,
                show_workspace_select=True,
                workspace=None,
                form_data=form_data,
                workspace_documents=workspace_documents,
            )

        # Parse date/time
        try:
            dt = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")
        except Exception:
            flash("Invalid date/time format. Use YYYY-MM-DD HH:MM.", "danger")
            workspaces = get_user_active_workspaces(current_user.user_id)
            workspace_documents = get_reference_documents_for_user(user.user_id, workspace_id)
            return render_template(
                "workshop_create.html",
                workspaces=workspaces,
                show_workspace_select=True,
                workspace=None,
                form_data=form_data,
                workspace_documents=workspace_documents,
            )

        if upload_files and workspace_id:
            uploaded_doc_ids, upload_warnings = _process_reference_uploads(
                upload_files,
                workspace_id=workspace_id,
                user_id=user.user_id,
            )

        # Create workshop
        try:
            w = Workshop()
            w.title = title
            w.objective = objective or None
            w.workspace_id = workspace_id
            w.date_time = dt
            w.duration = duration
            w.status = 'scheduled'
            w.created_by_id = user.user_id
            db.session.add(w)
            db.session.flush()

            all_document_ids = list(linked_document_ids)
            all_document_ids.extend(str(doc_id) for doc_id in uploaded_doc_ids)

            if all_document_ids:
                try:
                    attach_documents_to_workshop(w, all_document_ids)
                except Exception as doc_err:
                    current_app.logger.warning(
                        "Failed to attach documents to workshop %s: %s",
                        w.id,
                        doc_err,
                    )

            agenda_warning = None
            agenda_notice = None
            if auto_generate_agenda:
                try:
                    run_agenda_pipeline(
                        workshop=w,
                        agenda_draft=agenda,
                        auto_generate=True,
                    )
                    if getattr(w, "agenda_auto_generate", True) is False:
                        agenda_notice = (
                            "Agenda AI generation is disabled because AWS Bedrock credentials aren't configured. "
                            "We saved your draft exactly as typed."
                        )
                except AgendaGenerationError as pipeline_err:
                    current_app.logger.warning(
                        "Agenda pipeline failed during workshop creation (code=%s): %s",
                        getattr(pipeline_err, "code", None),
                        pipeline_err,
                    )
                    run_agenda_pipeline(
                        workshop=w,
                        agenda_draft=agenda,
                        auto_generate=False,
                    )
                    warning_msg, notice_msg = _agenda_flash_for_failure(pipeline_err)
                    agenda_warning = warning_msg
                    agenda_notice = notice_msg
            else:
                run_agenda_pipeline(
                    workshop=w,
                    agenda_draft=agenda,
                    auto_generate=False,
                )
                agenda_notice = "Auto-structure skipped. Your agenda draft was saved exactly as typed."

            # Seed plan items based on workshop type/template; default to brainstorm
            try:
                _seed_plan_for_workshop(w)
            except Exception as e:
                current_app.logger.warning(f"Plan seeding failed for new workshop {w.id}: {e}")

            # Add organizer as participant
            organizer = WorkshopParticipant()
            organizer.workshop_id = w.id
            organizer.user_id = user.user_id
            organizer.role = 'organizer'
            organizer.status = 'accepted'
            organizer.joined_timestamp = datetime.utcnow()
            db.session.add(organizer)
            db.session.commit()
            if upload_warnings:
                for warning in upload_warnings:
                    flash(warning, "warning")
            if agenda_warning:
                flash(agenda_warning, "warning")
            if agenda_notice:
                flash(agenda_notice, "info")
            flash("Workshop created.", "success")
            return redirect(url_for('workshop_bp.view_workshop', workshop_id=w.id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating workshop: {e}")
            flash("An error occurred while creating the workshop.", "danger")
            workspaces = get_user_active_workspaces(user.user_id)
            workspace_documents = get_reference_documents_for_user(user.user_id, workspace_id)
            return render_template(
                "workshop_create.html",
                workspaces=workspaces,
                show_workspace_select=True,
                workspace=None,
                form_data=form_data,
                workspace_documents=workspace_documents,
            )

    # GET
    workspaces = get_user_active_workspaces(user.user_id)
    workspace_documents = get_reference_documents_for_user(user.user_id)
    return render_template(
        "workshop_create.html",
        workspaces=workspaces,
        show_workspace_select=True,
        workspace=None,
        form_data=None,
        workspace_documents=workspace_documents,
    )


@workshop_bp.route("/create/<int:workspace_id>", methods=["GET", "POST"])
@login_required
def create_workshop_specific(workspace_id: int) -> ResponseReturnValue:
    """Create a workshop within a specific workspace context."""
    workspace = Workspace.query.get_or_404(workspace_id)
    user = cast(User, current_user)
    membership = WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=user.user_id, status='active').first()
    if not membership and workspace.owner_id != user.user_id:
        flash("You are not a member of this workspace.", "danger")
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        objective = (request.form.get("objective") or "").strip()
        date_time_str = (request.form.get("date_time") or "").strip()
        duration = request.form.get("duration", type=int)
        agenda = (request.form.get("agenda") or "").strip()
        auto_generate_agenda = (request.form.get("auto_generate_agenda") == "on")
        linked_document_ids = [value for value in request.form.getlist("linked_document_ids") if value]
        upload_files = request.files.getlist("reference_uploads")
        uploaded_doc_ids: list[int] = []
        upload_warnings: list[str] = []

        form_data = type("obj", (), dict(
            title=title,
            objective=objective,
            date_time=date_time_str,
            duration=duration if duration is not None else "",
            agenda=agenda,
            auto_generate_agenda=auto_generate_agenda,
            linked_document_ids=linked_document_ids,
        ))

        if not title or not date_time_str:
            flash("Title and date/time are required.", "danger")
            workspace_documents = get_reference_documents_for_user(user.user_id, workspace_id)
            return render_template(
                "workshop_create.html",
                workspace=workspace,
                workspaces=None,
                show_workspace_select=False,
                form_data=form_data,
                workspace_documents=workspace_documents,
            )
        try:
            dt = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")
        except Exception:
            flash("Invalid date/time format. Use YYYY-MM-DD HH:MM.", "danger")
            workspace_documents = get_reference_documents_for_user(user.user_id, workspace_id)
            return render_template(
                "workshop_create.html",
                workspace=workspace,
                workspaces=None,
                show_workspace_select=False,
                form_data=form_data,
                workspace_documents=workspace_documents,
            )

        if upload_files:
            uploaded_doc_ids, upload_warnings = _process_reference_uploads(
                upload_files,
                workspace_id=workspace_id,
                user_id=user.user_id,
            )

        try:
            w = Workshop()
            w.title = title
            w.objective = objective or None
            w.workspace_id = workspace_id
            w.date_time = dt
            w.duration = duration
            w.status = 'scheduled'
            w.created_by_id = user.user_id
            db.session.add(w)
            db.session.flush()

            all_document_ids = list(linked_document_ids)
            all_document_ids.extend(str(doc_id) for doc_id in uploaded_doc_ids)

            if all_document_ids:
                try:
                    attach_documents_to_workshop(w, all_document_ids)
                except Exception as doc_err:
                    current_app.logger.warning(
                        "Failed to attach documents to workshop %s: %s",
                        w.id,
                        doc_err,
                    )

            agenda_warning = None
            agenda_notice = None
            if auto_generate_agenda:
                try:
                    run_agenda_pipeline(
                        workshop=w,
                        agenda_draft=agenda,
                        auto_generate=True,
                    )
                    if getattr(w, "agenda_auto_generate", True) is False:
                        agenda_notice = (
                            "Agenda AI generation is disabled because AWS Bedrock credentials aren't configured. "
                            "We saved your draft exactly as typed."
                        )
                except AgendaGenerationError as pipeline_err:
                    current_app.logger.warning(
                        "Agenda pipeline failed during specific workshop creation (code=%s): %s",
                        getattr(pipeline_err, "code", None),
                        pipeline_err,
                    )
                    run_agenda_pipeline(
                        workshop=w,
                        agenda_draft=agenda,
                        auto_generate=False,
                    )
                    warning_msg, notice_msg = _agenda_flash_for_failure(pipeline_err)
                    agenda_warning = warning_msg
                    agenda_notice = notice_msg
            else:
                run_agenda_pipeline(
                    workshop=w,
                    agenda_draft=agenda,
                    auto_generate=False,
                )
                agenda_notice = "Auto-structure skipped. Your agenda draft was saved exactly as typed."

            # Seed plan items based on workshop type/template; default to brainstorm
            try:
                _seed_plan_for_workshop(w)
            except Exception as e:
                current_app.logger.warning(f"Plan seeding failed for new workshop {w.id}: {e}")

            organizer = WorkshopParticipant()
            organizer.workshop_id = w.id
            organizer.user_id = user.user_id
            organizer.role = 'organizer'
            organizer.status = 'accepted'
            organizer.joined_timestamp = datetime.utcnow()
            db.session.add(organizer)
            db.session.commit()
            if upload_warnings:
                for warning in upload_warnings:
                    flash(warning, "warning")
            if agenda_warning:
                flash(agenda_warning, "warning")
            if agenda_notice:
                flash(agenda_notice, "info")
            flash("Workshop created.", "success")
            return redirect(url_for('workshop_bp.view_workshop', workshop_id=w.id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating workshop in workspace {workspace_id}: {e}")
            flash("An error occurred while creating the workshop.", "danger")
            workspace_documents = get_reference_documents_for_user(user.user_id, workspace_id)
            return render_template(
                "workshop_create.html",
                workspace=workspace,
                workspaces=None,
                show_workspace_select=False,
                form_data=form_data,
                workspace_documents=workspace_documents,
            )

    # GET
    workspace_documents = get_reference_documents_for_user(user.user_id, workspace_id)
    return render_template(
        "workshop_create.html",
        workspace=workspace,
        workspaces=None,
        show_workspace_select=False,
        form_data=None,
        workspace_documents=workspace_documents,
    )


@workshop_bp.route("/<int:workshop_id>")
@login_required
def view_workshop(workshop_id: int) -> ResponseReturnValue:
    """Workshop details page with participants and docs."""
    # Load workshop record; avoid ORM loader hints that trigger strict type-checker errors
    workshop = Workshop.query.get_or_404(workshop_id)

    # Access control: organizer, participant, or active workspace member
    user = cast(User, current_user)
    is_org = (workshop.created_by_id == user.user_id)
    participant = WorkshopParticipant.query.filter_by(workshop_id=workshop.id, user_id=user.user_id).first()
    workspace_membership = WorkspaceMember.query.filter_by(workspace_id=workshop.workspace_id, user_id=user.user_id, status='active').first()
    if not (is_org or participant or workspace_membership):
        flash("You do not have access to this workshop.", "danger")
        return redirect(url_for('workshop_bp.list_workshops'))

    # Load related collections
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()
    linked_docs = WorkshopDocument.query.filter_by(workshop_id=workshop.id).all()

    # Organizer helpers
    user_is_organizer = is_org
    user_is_invited = bool(participant and participant.status == 'invited')
    user_is_accepted_participant = bool(participant and participant.status == 'accepted')
    user_can_request_participation = (not participant) and bool(workspace_membership)
    pending_participation_requests = []
    if user_is_organizer:
        pending_participation_requests = [p for p in participants if p.status == 'requested']

    # Potential participants (workspace members not yet participants)
    potential_participants = []
    if user_is_organizer and workspace_membership:
        member_users = [m.user for m in WorkspaceMember.query.filter_by(workspace_id=workshop.workspace_id, status='active').all()]
        existing_user_ids = {p.user_id for p in participants}
        potential_participants = [u for u in member_users if u.user_id not in existing_user_ids]

    # Available documents from workspace not yet linked
    available_documents = []
    try:
        linked_doc_ids = {ld.document_id for ld in linked_docs}
        q = Document.query.filter_by(workspace_id=workshop.workspace_id)
        if linked_doc_ids:
            q = q.filter(~Document.id.in_(linked_doc_ids))
        available_documents = q.order_by(Document.uploaded_at.desc()).all()
    except Exception:
        available_documents = []

    # For form prefilling in edit route, we reuse date_time_str property on object
    workshop.date_time_str = workshop.date_time.strftime('%Y-%m-%d %H:%M') if workshop.date_time else ''

    return render_template(
        "workshop_details.html",
        workshop=workshop,
        participants=participants,
        linked_documents=linked_docs,
        potential_participants=potential_participants,
        available_documents=available_documents,
        user_is_organizer=user_is_organizer,
        user_is_invited=user_is_invited,
        user_can_request_participation=user_can_request_participation,
        user_is_accepted_participant=user_is_accepted_participant,
        pending_participation_requests=pending_participation_requests,
    )


@workshop_bp.route("/edit/<int:workshop_id>", methods=["GET", "POST"])
@login_required
def edit_workshop(workshop_id: int) -> ResponseReturnValue:
    workshop = Workshop.query.get_or_404(workshop_id)
    user = cast(User, current_user)
    if workshop.created_by_id != user.user_id:
        flash("Only the organizer can edit this workshop.", "danger")
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))

    if request.method == 'POST':
        # Identify which form section was submitted to avoid unintended field resets
        form_section = (request.form.get('form_section') or 'core').strip()

        # Core fields (always included via hidden inputs in sub-forms)
        title = (request.form.get('title') or '').strip()
        objective = (request.form.get('objective') or '').strip()
        date_time_str = (request.form.get('date_time') or '').strip()
        duration = request.form.get('duration', type=int)
        status = (request.form.get('status') or '').strip() or workshop.status
        agenda = (request.form.get('agenda') or '').strip()

        # Helpers to parse checkbox values only if that checkbox existed in the submitted form
        def _checkbox_value(name: str) -> bool | None:
            present_key = f"{name}_present"
            if present_key in request.form:
                return request.form.get(name) == 'on'
            return None  # Not part of this form submission

        # Feature flags
        conference_active_flag = _checkbox_value('conference_active')
        transcription_enabled_flag = _checkbox_value('transcription_enabled')
        participant_delete_flag = _checkbox_value('participant_delete_enabled')

        # TTS settings (only apply when TTS form is posted)
        tts_provider = (request.form.get('tts_provider') or '').strip() or None
        tts_voice = (request.form.get('tts_voice') or '').strip() or None
        tts_speed_default_raw = request.form.get('tts_speed_default')
        tts_speed_default = _safe_float(tts_speed_default_raw)
        # TTS auto-read flag
        tts_autoread_enabled_flag = _checkbox_value('tts_autoread_enabled')

        if not title or not date_time_str:
            flash('Title and date/time are required.', 'danger')
            workshop.date_time_str = workshop.date_time.strftime('%Y-%m-%d %H:%M') if workshop.date_time else ''
            form_data = type('obj', (), dict(title=title, objective=objective, date_time=date_time_str, duration=duration, status=status, agenda=agenda))
            return render_template('workshop_edit.html', workshop=workshop, form_data=form_data)
        try:
            dt = datetime.strptime(date_time_str, '%Y-%m-%d %H:%M')
        except Exception:
            flash('Invalid date/time format. Use YYYY-MM-DD HH:MM.', 'danger')
            workshop.date_time_str = workshop.date_time.strftime('%Y-%m-%d %H:%M') if workshop.date_time else ''
            form_data = type('obj', (), dict(title=title, objective=objective, date_time=date_time_str, duration=duration, status=status, agenda=agenda))
            return render_template('workshop_edit.html', workshop=workshop, form_data=form_data)

        try:
            # Always update core fields (present in all forms via hidden inputs)
            workshop.title = title
            workshop.objective = objective or None
            workshop.date_time = dt
            workshop.duration = duration
            workshop.status = status
            workshop.agenda = agenda or None

            # Update feature flags only if they were part of this submission
            if hasattr(workshop, 'conference_active') and (conference_active_flag is not None):
                workshop.conference_active = conference_active_flag
            if hasattr(workshop, 'transcription_enabled') and (transcription_enabled_flag is not None):
                workshop.transcription_enabled = transcription_enabled_flag
            if hasattr(workshop, 'participant_can_delete_transcripts') and (participant_delete_flag is not None):
                workshop.participant_can_delete_transcripts = participant_delete_flag

            # Update TTS settings only when the TTS form was submitted
            if form_section == 'tts':
                if hasattr(workshop, 'tts_provider'):
                    workshop.tts_provider = tts_provider
                if hasattr(workshop, 'tts_voice'):
                    workshop.tts_voice = tts_voice
                if hasattr(workshop, 'tts_speed_default'):
                    workshop.tts_speed_default = tts_speed_default
            # Update TTS auto-read only if checkbox existed in this submission
            if hasattr(workshop, 'tts_autoread_enabled') and (tts_autoread_enabled_flag is not None):
                workshop.tts_autoread_enabled = tts_autoread_enabled_flag
            db.session.commit()
            flash('Workshop updated.', 'success')
            return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Error updating workshop {workshop_id}: {e}')
            flash('Error updating workshop.', 'danger')
            workshop.date_time_str = workshop.date_time.strftime('%Y-%m-%d %H:%M') if workshop.date_time else ''
            form_data = type('obj', (), dict(title=title, objective=objective, date_time=date_time_str, duration=duration, status=status, agenda=agenda))
            return render_template('workshop_edit.html', workshop=workshop, form_data=form_data)

    # GET
    workshop.date_time_str = workshop.date_time.strftime('%Y-%m-%d %H:%M') if workshop.date_time else ''
    # Provide participants for config dropdowns (e.g., speech/presentation)
    try:
        parts = WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()
        participants_for_config = []
        for p in parts:
            u = getattr(p, 'user', None)
            # Build a readable display name
            try:
                dn = None
                if u:
                    if getattr(u, 'first_name', None) or getattr(u, 'last_name', None):
                        fn = (getattr(u, 'first_name', '') or '').strip()
                        ln = (getattr(u, 'last_name', '') or '').strip()
                        dn = (fn + (' ' + ln if ln else '')).strip()
                    if not dn:
                        em = getattr(u, 'email', None)
                        if em:
                            dn = (em.split('@')[0] or em)
                if not dn:
                    dn = f"User {p.user_id}"
            except Exception:
                dn = f"User {getattr(p, 'user_id', '0')}"
            participants_for_config.append({
                'user_id': getattr(p, 'user_id', None),
                'display_name': dn
            })
    except Exception:
        participants_for_config = []
    # Provide linked documents for presentation config dropdowns
    try:
        links = WorkshopDocument.query.filter_by(workshop_id=workshop.id).all()
        docs_for_config = []
        for link in links:
            try:
                d = getattr(link, 'document', None)
                if d is None:
                    # Fallback fetch if relationship not loaded
                    d = Document.query.filter_by(id=link.document_id).first()
                if d:
                    title = getattr(d, 'title', None) or getattr(d, 'file_name', None) or f"Document {d.id}"
                    docs_for_config.append({ 'id': d.id, 'title': title })
            except Exception:
                pass
    except Exception:
        docs_for_config = []

    return render_template('workshop_edit.html', workshop=workshop, form_data=None, participants_for_config=participants_for_config, docs_for_config=docs_for_config)


@workshop_bp.route('/<int:workshop_id>/plan', methods=['GET'])
@login_required
def get_workshop_plan(workshop_id: int) -> ResponseReturnValue:
    ws = Workshop.query.get_or_404(workshop_id)
    # For now, any authenticated viewer of the workshop can read the plan
    from app.tasks.registry import TASK_REGISTRY
    from app.config import TASK_SEQUENCE
    from app.models import WorkshopPlanItem
    # Always prefer normalized DB-backed plan items. If missing, migrate/populate once.
    if not (ws.plan_items and len(ws.plan_items) > 0):
        # Try to migrate any legacy JSON into DB; else seed defaults from TASK_SEQUENCE
        try:
            raw = json.loads(ws.task_sequence) if ws.task_sequence else None
        except Exception:
            raw = None

        def _normalize_from_json(raw_json: Any) -> list[dict[str, Any]]:
            nodes: list[dict[str, Any]] = []
            if isinstance(raw_json, list):
                for item in raw_json:
                    if isinstance(item, str):
                        t = item
                        if t in TASK_REGISTRY:
                            nodes.append({
                                "task_type": t,
                                "duration": int(TASK_REGISTRY[t].get("default_duration", 60)),
                                "phase": None,
                                "description": None,
                                "enabled": True,
                            })
                    elif isinstance(item, dict):
                        t_val = item.get("task_type") or item.get("type") or item.get("phase")
                        if isinstance(t_val, str) and t_val in TASK_REGISTRY:
                            dflt = int(TASK_REGISTRY[t_val].get("default_duration", 60))
                            duration_value = _safe_int(item.get("duration"), default=dflt)
                            duration_value = max(30, min(7200, duration_value)) if duration_value else dflt
                            nodes.append({
                                "task_type": t_val,
                                "duration": duration_value,
                                "phase": item.get("phase"),
                                "description": item.get("description"),
                                "enabled": bool(item.get("enabled", True)),
                            })
            return nodes

        nodes = _normalize_from_json(raw) if raw is not None else _serialize_default_plan()
        try:
            # Write into DB in order and mirror JSON for back-compat
            from app.models import WorkshopPlanItem
            WorkshopPlanItem.query.filter_by(workshop_id=ws.id).delete()
            for idx, node in enumerate(nodes):
                if not isinstance(node, Mapping):
                    continue
                t_raw = node.get("task_type")
                if not isinstance(t_raw, str) or not t_raw:
                    continue
                dur_val = node.get("duration")
                d = _safe_int(dur_val, default=0)
                phase_raw = node.get("phase")
                ph = str(phase_raw) if isinstance(phase_raw, str) and phase_raw else None
                desc_raw = node.get("description")
                desc = str(desc_raw) if isinstance(desc_raw, str) and desc_raw else None
                en = bool(node.get("enabled", True))
                it = WorkshopPlanItem()
                it.workshop_id = ws.id
                # Replace DB-backed items transactionally 
                it.order_index = idx
                it.task_type = t_raw
                it.duration = d
                it.phase = ph
                it.description = desc
                # Persist optional config_json if present
                cfg = node.get("config_json")
                if cfg is not None:
                    try:
                        it.config_json = json.dumps(cfg) if not isinstance(cfg, str) else cfg
                    except Exception:
                        it.config_json = None
                it.enabled = en
                db.session.add(it)
            ws.task_sequence = json.dumps(nodes)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Plan seed/migration failed for workshop {workshop_id}: {e}")

    # Return DB-backed plan items (fresh query to avoid stale relationship cache)
    items = (
        WorkshopPlanItem.query
        .filter_by(workshop_id=ws.id)
        .order_by(WorkshopPlanItem.order_index.asc())
        .all()
    )
    current = [{
        "id": it.id,
        "order_index": it.order_index,
        "task_type": it.task_type,
        "duration": int(it.duration) if it.duration is not None else 0,
        "phase": it.phase,
        "description": it.description,
        "config_json": (json.loads(it.config_json) if getattr(it, 'config_json', None) else None),
        "enabled": bool(it.enabled),
    } for it in items]
    if not current:
        # Safety fallback if DB is empty due to a prior failure: serve defaults so UI isn't blank
        try:
            current_app.logger.warning(f"Plan DB empty for workshop {workshop_id}; serving defaults.")
        except Exception:
            pass
        current = _serialize_default_plan()
    # Prefer available tasks in TASK_SEQUENCE order to keep defaults aligned with UX
    ordered = [t for t in TASK_SEQUENCE if t in TASK_REGISTRY]
    # Include newly added generic types at the end for discoverability if not already in TASK_SEQUENCE
    for extra in ("meeting", "presentation", "speech", "vote_generic"):
        if extra in TASK_REGISTRY and extra not in ordered:
            ordered.append(extra)
    if not ordered:
        # Hard fallback to common phases if registry is unexpectedly empty
        ordered = [
            "brainstorming",
            "clustering_voting",
            "results_feasibility",
            "discussion",
            "summary",
        ]
    available_tasks = []
    for t in ordered:
        meta = TASK_REGISTRY.get(t) or {}
        available_tasks.append({
            "task_type": t,
            "default_duration": int(meta.get("default_duration", 60)),
            "inputs": meta.get("inputs", []),
            "outputs": meta.get("outputs", []),
        })
    # Build a non-persistent default plan (with config defaults) for UI reset/seed usage
    try:
        default_plan = _build_default_plan_for_workshop(ws)
    except Exception:
        default_plan = _serialize_default_plan()
    return jsonify({
        "available_tasks": available_tasks,
        "current_plan": current,
        "default_plan": default_plan,
        "source": "db"
    })

    # Always return a normalized list of plan nodes from legacy store otherwise.
    def _normalize(raw) -> list[dict]:
        nodes: list[dict] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    t = item
                    if t in TASK_REGISTRY:
                        nodes.append({
                            "task_type": t,
                            "duration": int(TASK_REGISTRY[t].get("default_duration", 60)),
                            "phase": None,
                            "description": None,
                            "enabled": True,
                        })
                elif isinstance(item, dict):
                    t = item.get("task_type") or item.get("type") or item.get("phase")
                    if t in TASK_REGISTRY:
                        if item.get("enabled") is False:
                            # Keep disabled nodes so the editor can toggle; they will be filtered in read-only views.
                            dflt = int(TASK_REGISTRY[t].get("default_duration", 60))
                            try:
                                d = int(item.get("duration", dflt))
                            except Exception:
                                d = dflt
                            d = max(30, min(7200, d))
                            nodes.append({
                                "task_type": t,
                                "duration": d,
                                "phase": item.get("phase"),
                                "description": item.get("description"),
                                "enabled": False,
                            })
                        else:
                            dflt = int(TASK_REGISTRY[t].get("default_duration", 60))
                            try:
                                d = int(item.get("duration", dflt))
                            except Exception:
                                d = dflt
                            d = max(30, min(7200, d))
                            nodes.append({
                                "task_type": t,
                                "duration": d,
                                "phase": item.get("phase"),
                                "description": item.get("description"),
                                "enabled": True,
                            })
        return nodes

    try:
        raw = json.loads(ws.task_sequence) if ws.task_sequence else None
        current = _normalize(raw) if raw is not None else _serialize_default_plan()
        if not current:
            current = _serialize_default_plan()
    except Exception:
        current = _serialize_default_plan()
    ordered = [t for t in TASK_SEQUENCE if t in TASK_REGISTRY]
    available_tasks = [
        {"task_type": t, "default_duration": int(TASK_REGISTRY[t].get("default_duration", 60))}
        for t in ordered
    ]
    return jsonify({
        "available_tasks": available_tasks,
        "current_plan": current,
        "source": "json"
    })


@workshop_bp.route('/<int:workshop_id>/plan', methods=['POST'])
@login_required
def set_workshop_plan(workshop_id: int) -> ResponseReturnValue:
    ws = Workshop.query.get_or_404(workshop_id)
    # In tests, LOGIN_DISABLED may be set; allow updates unconditionally then.
    if not current_app.config.get('LOGIN_DISABLED') and ws.created_by_id != current_user.user_id:
        return jsonify({"error": "Only the organizer can update the plan."}), 403
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400
    ok, res = _validate_plan(payload)
    if not ok:
        return jsonify({"error": res}), 400
    try:
        # Replace DB-backed items transactionally
        # Clear existing items
        WorkshopPlanItem.query.filter_by(workshop_id=ws.id).delete()
        db.session.flush()
        # Insert new ones preserving order
        for idx, node in enumerate(res):
            if not isinstance(node, Mapping):
                continue
            task_type_raw = node.get("task_type")
            if not isinstance(task_type_raw, str) or not task_type_raw:
                continue
            it = WorkshopPlanItem()
            it.workshop_id = ws.id 
            it.order_index = idx
            it.task_type = task_type_raw
            it.duration = _safe_int(node.get("duration"), default=0)
            phase_val = node.get("phase")
            it.phase = str(phase_val) if isinstance(phase_val, str) and phase_val else None
            desc_val = node.get("description")
            it.description = str(desc_val) if isinstance(desc_val, str) and desc_val else None
            # Accept config_json and normalize for known types to enforce allowed fields
            cfg_raw = node.get("config_json")
            if cfg_raw is not None:
                try:
                    cfg_obj: Any = cfg_raw
                    if isinstance(cfg_raw, str):
                        cfg_obj = json.loads(cfg_raw)
                except Exception:
                    cfg_obj = None
                # Normalize based on task type
                ttype = (it.task_type or '').lower()
                try:
                    if ttype == 'presentation':
                        normalized: dict[str, Any] = {}
                        # presenter_user_id (optional, must be a participant)
                        presenter_raw = cfg_obj.get('presenter_user_id') if isinstance(cfg_obj, Mapping) else None
                        if presenter_raw not in (None, ''):
                            try:
                                presenter_id = int(presenter_raw)  # type: ignore[arg-type]
                                from app.models import WorkshopParticipant as _WP
                                sp = _WP.query.filter_by(workshop_id=ws.id, user_id=presenter_id).first()
                                if sp:
                                    normalized['presenter_user_id'] = presenter_id
                            except Exception:
                                pass
                        # document_id (optional, must be linked)
                        doc_raw = cfg_obj.get('document_id') if isinstance(cfg_obj, Mapping) else None
                        if doc_raw not in (None, ''):
                            try:
                                doc_id = int(doc_raw)  # type: ignore[arg-type]
                                link = WorkshopDocument.query.filter_by(workshop_id=ws.id, document_id=doc_id).first()
                                if link:
                                    normalized['document_id'] = doc_id
                            except Exception:
                                pass
                        # initial_slide_index (>=1, default 1 if provided)
                        idx_raw = cfg_obj.get('initial_slide_index') if isinstance(cfg_obj, Mapping) else None
                        if idx_raw not in (None, ''):
                            try:
                                idxv = int(idx_raw)  # type: ignore[arg-type]
                                normalized['initial_slide_index'] = max(1, idxv)
                            except Exception:
                                normalized['initial_slide_index'] = 1
                        elif isinstance(cfg_obj, dict) and ('presenter_user_id' in cfg_obj or 'document_id' in cfg_obj):
                            # If any presentation fields present, set default index
                            normalized['initial_slide_index'] = 1
                        it.config_json = json.dumps(normalized) if normalized else None
                    elif ttype == 'results_prioritization':
                        normalized = {}
                        if isinstance(cfg_obj, Mapping) and 'weights' in cfg_obj and isinstance(cfg_obj.get('weights'), Mapping):
                            weights = cfg_obj.get('weights') if isinstance(cfg_obj, Mapping) else None
                            w: dict[str, float] = {}
                            if isinstance(weights, Mapping):
                                for k in ('votes', 'feasibility', 'objective_fit'):
                                    candidate = weights.get(k)
                                    converted = _safe_float(candidate)
                                    if converted is not None:
                                        w[k] = converted
                            if w:
                                normalized['weights'] = w
                        if isinstance(cfg_obj, Mapping) and 'constraints' in cfg_obj and isinstance(cfg_obj.get('constraints'), Mapping):
                            constraints = cfg_obj.get('constraints') if isinstance(cfg_obj, Mapping) else None
                            c: dict[str, int] = {}
                            if isinstance(constraints, Mapping) and constraints.get('max_items') not in (None, ''):
                                max_items = _safe_int(constraints.get('max_items'), default=1)
                                c['max_items'] = max(1, max_items)
                            if c:
                                normalized['constraints'] = c
                        it.config_json = json.dumps(normalized) if normalized else None
                    elif ttype == 'results_action_plan':
                        # No configurable fields supported currently
                        it.config_json = None
                    else:
                        # Default: store as-is
                        try:
                            it.config_json = json.dumps(cfg_obj) if not isinstance(cfg_raw, str) else cfg_raw
                        except Exception:
                            it.config_json = None
                except Exception:
                    # On any normalization error, drop config for safety
                    it.config_json = None
            it.enabled = True
            if not it.task_type:
                continue
            db.session.add(it)
        # Optional: also mirror JSON for backward compatibility 
        ws.task_sequence = json.dumps(res)
        db.session.commit()
        return jsonify({"status": "ok", "source": "db"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to save plan for workshop {workshop_id}: {e}")
        return jsonify({"error": "Failed to save plan"}), 500 


@workshop_bp.route('/<int:workshop_id>/plan/items/<int:item_id>', methods=['GET'])
@login_required
def get_plan_item(workshop_id: int, item_id: int) -> ResponseReturnValue:
    ws = Workshop.query.get_or_404(workshop_id)
    # Organizer or any workshop viewer can read
    it = WorkshopPlanItem.query.filter_by(id=item_id, workshop_id=ws.id).first_or_404()
    return jsonify({
        "id": it.id,
        "order_index": it.order_index,
        "task_type": it.task_type,
        "duration": int(it.duration) if it.duration is not None else 0,
        "phase": it.phase,
        "description": it.description,
        "config_json": (json.loads(it.config_json) if getattr(it, 'config_json', None) else None),
        "enabled": bool(it.enabled),
    })


@workshop_bp.route('/<int:workshop_id>/plan/items/<int:item_id>', methods=['PATCH'])
@login_required
def patch_plan_item(workshop_id: int, item_id: int) -> ResponseReturnValue:
    ws = Workshop.query.get_or_404(workshop_id)
    user = cast(User, current_user)
    if not current_app.config.get('LOGIN_DISABLED') and ws.created_by_id != user.user_id:
        return jsonify({"error": "Only the organizer can update the plan."}), 403
    it = WorkshopPlanItem.query.filter_by(id=item_id, workshop_id=ws.id).first_or_404()
    data = request.get_json(silent=True) or {}
    try:
        if 'order_index' in data:
            try:
                oi = data.get('order_index')
                if oi is not None:
                    it.order_index = int(oi)
            except Exception:
                pass
        if 'duration' in data:
            try:
                d = int(data.get('duration') or 0)
                if d > 0:
                    d = max(30, min(7200, d))
                it.duration = d
            except Exception:
                pass
        if 'phase' in data:
            it.phase = (data.get('phase') or None)
        if 'description' in data:
            # Allow raw text or JSON for config-like tasks; UI can use config_json instead
            it.description = (data.get('description') or None)
        if 'config_json' in data:
            cfg_raw = data.get('config_json')
            # For certain task types, validate/normalize config before persisting
            task_type_lower = (it.task_type or '').lower()
            if task_type_lower == 'speech':
                try:
                    cfg_obj: Any = cfg_raw
                    if isinstance(cfg_raw, str):
                        cfg_obj = json.loads(cfg_raw)
                    if not isinstance(cfg_obj, dict):
                        return jsonify({"error": "Invalid config_json for speech."}), 400
                    speech_config: dict[str, Any] = {}
                    delivery_mode_raw = cfg_obj.get('delivery_mode')
                    delivery_mode = str(delivery_mode_raw or 'direct').strip().lower()
                    if delivery_mode not in ('direct', 'reader'):
                        return jsonify({"error": "delivery_mode must be one of: direct, reader."}), 400
                    speech_config['delivery_mode'] = delivery_mode
                    speaker_val = cfg_obj.get('speaker_user_id') if 'speaker_user_id' in cfg_obj else None
                    if speaker_val not in (None, ''):
                        if not isinstance(speaker_val, (int, str)):
                            return jsonify({"error": "speaker_user_id must be an integer."}), 400
                        try:
                            speaker_id = int(speaker_val)
                        except (ValueError, TypeError):
                            return jsonify({"error": "speaker_user_id must be an integer."}), 400
                        from app.models import WorkshopParticipant  # inline import to avoid cycles
                        sp = WorkshopParticipant.query.filter_by(workshop_id=ws.id, user_id=speaker_id).first()
                        if not sp:
                            return jsonify({"error": "Selected speaker is not a participant in this workshop."}), 400
                        speech_config['speaker_user_id'] = speaker_id
                    if 'cc_enabled' in cfg_obj:
                        speech_config['cc_enabled'] = bool(cfg_obj.get('cc_enabled'))
                    if 'duration_sec' in cfg_obj and cfg_obj.get('duration_sec') not in (None, ''):
                        val = cfg_obj.get('duration_sec')
                        if not isinstance(val, (int, str)):
                            return jsonify({"error": "duration_sec must be an integer number of seconds."}), 400
                        try:
                            dur = int(val)
                        except (ValueError, TypeError):
                            return jsonify({"error": "duration_sec must be an integer number of seconds."}), 400
                        if dur < 30 or dur > 7200:
                            return jsonify({"error": "duration_sec out of range (30-7200)."}), 400
                        speech_config['duration_sec'] = dur
                    if delivery_mode == 'reader':
                        if 'document_id' in cfg_obj and cfg_obj.get('document_id') not in (None, ''):
                            doc_candidate = cfg_obj.get('document_id')
                            if not isinstance(doc_candidate, (int, str)):
                                return jsonify({"error": "document_id must be an integer."}), 400
                            try:
                                doc_id = int(doc_candidate)
                            except (ValueError, TypeError):
                                return jsonify({"error": "document_id must be an integer."}), 400
                            link = WorkshopDocument.query.filter_by(workshop_id=ws.id, document_id=doc_id).first()
                            if not link:
                                return jsonify({"error": "Selected document is not linked to this workshop."}), 400
                            speech_config['document_id'] = doc_id
                        if 'script_text' in cfg_obj and cfg_obj.get('script_text') is not None:
                            script_text = str(cfg_obj.get('script_text'))
                            speech_config['script_text'] = script_text[:20000]
                    it.config_json = json.dumps(speech_config)
                except Exception as _e:
                    current_app.logger.warning(f"Failed to validate speech config for item {item_id}: {_e}")
                    return jsonify({"error": "Invalid speech configuration."}), 400
            elif task_type_lower == 'framing':
                try:
                    cfg_obj = cfg_raw
                    if isinstance(cfg_raw, str):
                        cfg_obj = json.loads(cfg_raw)
                    if not isinstance(cfg_obj, dict):
                        return jsonify({"error": "Invalid config_json for framing."}), 400
                    framing_config: dict[str, Any] = {}
                    if 'duration_sec' in cfg_obj and cfg_obj.get('duration_sec') not in (None, ''):
                        val = cfg_obj.get('duration_sec')
                        if not isinstance(val, (int, str)):
                            return jsonify({"error": "duration_sec must be an integer number of seconds."}), 400
                        try:
                            dur = int(val)
                        except (ValueError, TypeError):
                            return jsonify({"error": "duration_sec must be an integer number of seconds."}), 400
                        if dur < 30 or dur > 7200:
                            return jsonify({"error": "duration_sec out of range (30-7200)."}), 400
                        framing_config['duration_sec'] = dur
                    if 'cc_enabled' in cfg_obj:
                        framing_config['cc_enabled'] = bool(cfg_obj.get('cc_enabled'))
                    if 'framing_prompt' in cfg_obj and cfg_obj.get('framing_prompt') is not None:
                        framing_config['framing_prompt'] = str(cfg_obj.get('framing_prompt'))[:8000]
                    if 'style' in cfg_obj and cfg_obj.get('style') is not None:
                        framing_config['style'] = str(cfg_obj.get('style'))[:256]
                    if 'audience' in cfg_obj and cfg_obj.get('audience') is not None:
                        framing_config['audience'] = str(cfg_obj.get('audience'))[:256]
                    if 'key_points' in cfg_obj and cfg_obj.get('key_points') is not None:
                        kp = cfg_obj.get('key_points')
                        if not isinstance(kp, list):
                            return jsonify({"error": "key_points must be a list of strings."}), 400
                        kps: list[str] = []
                        for item in kp[:15]:
                            try:
                                s = str(item).strip()
                            except Exception:
                                s = ''
                            if s:
                                kps.append(s[:512])
                        if kps:
                            framing_config['key_points'] = kps
                    it.config_json = json.dumps(framing_config)
                except Exception as _e:
                    current_app.logger.warning(f"Failed to validate framing config for item {item_id}: {_e}")
                    return jsonify({"error": "Invalid framing configuration."}), 400
            elif (it.task_type or '').lower() == 'presentation':
                try:
                    cfg_obj = cfg_raw
                    if isinstance(cfg_raw, str):
                        cfg_obj = json.loads(cfg_raw)
                    if not isinstance(cfg_obj, dict):
                        return jsonify({"error": "Invalid config_json for presentation."}), 400
                    presentation_config: dict[str, Any] = {}
                    # Slideshow-only; remove any legacy mode/knobs silently
                    # presenter_user_id: must be a participant in this workshop
                    presenter_val = cfg_obj.get('presenter_user_id') if 'presenter_user_id' in cfg_obj else None
                    if presenter_val not in (None, ''):
                        if not isinstance(presenter_val, (int, str)):
                            return jsonify({"error": "presenter_user_id must be an integer."}), 400
                        try:
                            presenter_id = int(presenter_val)
                        except (ValueError, TypeError):
                            return jsonify({"error": "presenter_user_id must be an integer."}), 400
                        from app.models import WorkshopParticipant as _WP
                        sp = _WP.query.filter_by(workshop_id=ws.id, user_id=presenter_id).first()
                        if not sp:
                            return jsonify({"error": "Selected presenter is not a participant in this workshop."}), 400
                        presentation_config['presenter_user_id'] = presenter_id
                    # document_id: must be linked to this workshop
                    doc_val = cfg_obj.get('document_id') if 'document_id' in cfg_obj else None
                    if doc_val not in (None, ''):
                        if not isinstance(doc_val, (int, str)):
                            return jsonify({"error": "document_id must be an integer."}), 400
                        try:
                            doc_id = int(doc_val)
                        except (ValueError, TypeError):
                            return jsonify({"error": "document_id must be an integer."}), 400
                        link = WorkshopDocument.query.filter_by(workshop_id=ws.id, document_id=doc_id).first()
                        if not link:
                            return jsonify({"error": "Selected document is not linked to this workshop."}), 400
                        presentation_config['document_id'] = doc_id
                    # initial_slide_index: default 1, clamp >=1
                    if 'initial_slide_index' in cfg_obj and cfg_obj.get('initial_slide_index') not in (None, ''):
                        try:
                            v = cfg_obj.get('initial_slide_index')
                            if not isinstance(v, (int, str)):
                                raise TypeError('initial_slide_index must be int or str')
                            idx = int(v)
                        except (ValueError, TypeError):
                            return jsonify({"error": "initial_slide_index must be an integer."}), 400
                        presentation_config['initial_slide_index'] = max(1, idx)
                    else:
                        presentation_config['initial_slide_index'] = 1
                    it.config_json = json.dumps(presentation_config)
                except Exception as _e:
                    current_app.logger.warning(f"Failed to validate presentation config for item {item_id}: {_e}")
                    return jsonify({"error": "Invalid presentation configuration."}), 400
            elif (it.task_type or '').lower() == 'results_prioritization':
                try:
                    cfg_obj = cfg_raw
                    if isinstance(cfg_raw, str):
                        cfg_obj = json.loads(cfg_raw)
                    if cfg_obj in (None, ''):
                        it.config_json = None
                    else:
                        if not isinstance(cfg_obj, dict):
                            return jsonify({"error": "Invalid config_json for results_prioritization."}), 400
                        prioritization_config: dict[str, Any] = {}
                        # Optional weights
                        if 'weights' in cfg_obj and isinstance(cfg_obj.get('weights'), dict):
                            w: dict[str, float] = {}
                            weights_cfg = cfg_obj.get('weights')
                            if isinstance(weights_cfg, dict):
                                for k in ('votes', 'feasibility', 'objective_fit'):
                                    converted = _safe_float(weights_cfg.get(k))
                                    if converted is not None:
                                        w[k] = converted
                            if w:
                                prioritization_config['weights'] = w
                        # Optional constraints
                        if 'constraints' in cfg_obj and isinstance(cfg_obj.get('constraints'), dict):
                            c: dict[str, int] = {}
                            constraints_cfg = cfg_obj.get('constraints')
                            if isinstance(constraints_cfg, dict) and constraints_cfg.get('max_items') not in (None, ''):
                                max_items = _safe_int(constraints_cfg.get('max_items'), default=1)
                                c['max_items'] = max(1, max_items)
                            if c:
                                prioritization_config['constraints'] = c
                        it.config_json = json.dumps(prioritization_config) if prioritization_config else None
                except Exception as _e:
                    current_app.logger.warning(f"Failed to validate results_prioritization config for item {item_id}: {_e}")
                    return jsonify({"error": "Invalid results_prioritization configuration."}), 400
            elif (it.task_type or '').lower() == 'results_action_plan':
                try:
                    # Currently no configurable fields beyond defaults
                    cfg_obj = cfg_raw
                    if isinstance(cfg_raw, str):
                        cfg_obj = json.loads(cfg_raw)
                    # Ignore payload and persist None to avoid confusion
                    it.config_json = None
                except Exception as _e:
                    current_app.logger.warning(f"Failed to validate results_action_plan config for item {item_id}: {_e}")
                    return jsonify({"error": "Invalid results_action_plan configuration."}), 400
            else:
                # Default behavior: store as-is
                try:
                    it.config_json = json.dumps(cfg_raw) if not isinstance(cfg_raw, str) else cfg_raw
                except Exception:
                    it.config_json = None
        if 'enabled' in data:
            it.enabled = bool(data.get('enabled'))
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to patch plan item {item_id} for workshop {workshop_id}: {e}")
        return jsonify({"error": "Failed to update plan item"}), 500


@workshop_bp.route('/delete/<int:workshop_id>', methods=['POST'])
@login_required
def delete_workshop(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if workshop.created_by_id != current_user.user_id:
        flash('Only the organizer can delete this workshop.', 'danger')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))
    try:
        _prepare_workshop_for_delete(workshop)
        db.session.delete(workshop)
        db.session.commit()
        flash('Workshop deleted.', 'success')
        return redirect(url_for('workshop_bp.list_workshops'))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error deleting workshop {workshop_id}: {e}')
        flash('Error deleting workshop.', 'danger')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))


###################################
# Participation Management
###################################

@workshop_bp.route('/<int:workshop_id>/request_participation', methods=['POST'])
@login_required
def request_participation(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    existing = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, user_id=current_user.user_id).first()
    if existing:
        flash('You already have a participant record for this workshop.', 'info')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))
    # Must be member of workspace
    member = WorkspaceMember.query.filter_by(workspace_id=workshop.workspace_id, user_id=current_user.user_id, status='active').first()
    if not member:
        flash('Join the workspace first.', 'warning')
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workshop.workspace_id))
    try:
        p = WorkshopParticipant()
        p.workshop_id = workshop_id
        p.user_id = current_user.user_id
        p.role = 'participant'
        p.status = 'requested'
        p.joined_timestamp = None
        db.session.add(p)
        db.session.commit()
        flash('Participation request sent to organizer.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error requesting participation for workshop {workshop_id}: {e}')
        flash('Error requesting participation.', 'danger')
    return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))


@workshop_bp.route('/<int:workshop_id>/participants/<int:participant_id>/approve', methods=['POST'])
@login_required
def approve_participation_request(workshop_id, participant_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if workshop.created_by_id != current_user.user_id:
        flash('Only the organizer can approve requests.', 'danger')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))
    p = WorkshopParticipant.query.filter_by(id=participant_id, workshop_id=workshop_id).first_or_404()
    try:
        p.status = 'accepted'
        p.joined_timestamp = datetime.utcnow()
        db.session.commit()
        flash('Participation approved.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error approving participant {participant_id} in workshop {workshop_id}: {e}')
        flash('Error approving participation.', 'danger')
    return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))


@workshop_bp.route('/<int:workshop_id>/participants/<int:participant_id>/reject', methods=['POST'])
@login_required
def reject_participation_request(workshop_id, participant_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if workshop.created_by_id != current_user.user_id:
        flash('Only the organizer can reject requests.', 'danger')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))
    p = WorkshopParticipant.query.filter_by(id=participant_id, workshop_id=workshop_id).first_or_404()
    try:
        db.session.delete(p)
        db.session.commit()
        flash('Participation request rejected.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error rejecting participant {participant_id} in workshop {workshop_id}: {e}')
        flash('Error rejecting participation.', 'danger')
    return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))


@workshop_bp.route('/<int:workshop_id>/speech/preview', methods=['POST'])
@login_required
def preview_speech(workshop_id: int) -> ResponseReturnValue:
    """Generate a non-persistent preview for a Speech task based on posted config.

    Body JSON: { config_json: { delivery_mode, speaker_user_id, cc_enabled?, duration_sec?,
                                document_id?/script_text? for reader },
                 phase_context?: string }

    Returns: { success: true, preview: { tts_script, tts_read_time_seconds, ... } } or 4xx JSON.
    """
    ws = Workshop.query.get_or_404(workshop_id)
    user = cast(User, current_user)
    if not is_organizer(ws, user):
        return jsonify({"success": False, "message": "Only the organizer can preview speech."}), 403
    try:
        data = request.get_json(silent=True) or {}
        cfg = data.get('config_json') or {}
        if not isinstance(cfg, dict):
            return jsonify({"success": False, "message": "config_json must be an object."}), 400
        # Lightweight normalization similar to PATCH validation but less strict
        delivery_mode = (cfg.get('delivery_mode') or 'direct').strip().lower()
        if delivery_mode not in ('direct', 'reader'):
            return jsonify({"success": False, "message": "delivery_mode must be direct or reader."}), 400
        # Optionally coerce numeric fields
        if 'speaker_user_id' in cfg and cfg.get('speaker_user_id') not in (None, ''):
            try: cfg['speaker_user_id'] = int(cfg['speaker_user_id'])
            except Exception: return jsonify({"success": False, "message": "speaker_user_id must be integer."}), 400
        if 'duration_sec' in cfg and cfg.get('duration_sec') not in (None, ''):
            try:
                d = int(cfg['duration_sec'])
            except Exception:
                return jsonify({"success": False, "message": "duration_sec must be integer."}), 400
            if d < 30 or d > 7200:
                return jsonify({"success": False, "message": "duration_sec out of range (30-7200)."}), 400
            cfg['duration_sec'] = d
        if delivery_mode == 'reader' and 'document_id' in cfg and cfg.get('document_id') not in (None, ''):
            try:
                cfg['document_id'] = int(cfg['document_id'])
            except Exception:
                return jsonify({"success": False, "message": "document_id must be integer."}), 400

        phase_context = data.get('phase_context')
        preview = build_speech_preview(workshop_id, cfg, phase_context)
        if isinstance(preview, dict) and preview.get('error'):
            return jsonify({"success": False, "message": preview.get('error')}), 400
        return jsonify({"success": True, "preview": preview})
    except Exception as e:
        current_app.logger.error(f"Error building speech preview for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error building preview."}), 500


@workshop_bp.route('/<int:workshop_id>/framing/preview', methods=['POST'])
@login_required
def preview_framing(workshop_id: int) -> ResponseReturnValue:
    """Generate a non-persistent preview for a Framing task based on posted config."""
    ws = Workshop.query.get_or_404(workshop_id)
    user = cast(User, current_user)
    if not is_organizer(ws, user):
        return jsonify({"success": False, "message": "Only the organizer can preview framing."}), 403
    try:
        data = request.get_json(silent=True) or {}
        cfg = data.get('config_json') or {}
        if not isinstance(cfg, dict):
            return jsonify({"success": False, "message": "config_json must be an object."}), 400

        normalized: dict[str, Any] = {}
        if 'duration_sec' in cfg and cfg.get('duration_sec') not in (None, ''):
            duration_raw = cfg.get('duration_sec')
            try:
                dur = int(duration_raw)  # type: ignore[arg-type]
            except Exception:
                return jsonify({"success": False, "message": "duration_sec must be integer."}), 400
            if dur < 30 or dur > 7200:
                return jsonify({"success": False, "message": "duration_sec out of range (30-7200)."}), 400
            normalized['duration_sec'] = dur
        if 'cc_enabled' in cfg:
            normalized['cc_enabled'] = bool(cfg.get('cc_enabled'))
        if 'framing_prompt' in cfg and cfg.get('framing_prompt') is not None:
            normalized['framing_prompt'] = str(cfg.get('framing_prompt'))[:8000]
        if 'style' in cfg and cfg.get('style') is not None:
            normalized['style'] = str(cfg.get('style'))[:256]
        if 'audience' in cfg and cfg.get('audience') is not None:
            normalized['audience'] = str(cfg.get('audience'))[:256]
        if 'key_points' in cfg and cfg.get('key_points') is not None:
            kp_raw = cfg.get('key_points')
            if not isinstance(kp_raw, list):
                return jsonify({"success": False, "message": "key_points must be a list."}), 400
            kps: list[str] = []
            for item in kp_raw[:15]:
                try:
                    s = str(item).strip()
                except Exception:
                    s = ''
                if s:
                    kps.append(s[:512])
            if kps:
                normalized['key_points'] = kps

        phase_context = data.get('phase_context')
        preview = build_framing_preview(workshop_id, normalized or cfg, phase_context)
        if isinstance(preview, dict) and preview.get('error'):
            return jsonify({"success": False, "message": preview.get('error')}), 400
        return jsonify({"success": True, "preview": preview})
    except Exception as e:
        current_app.logger.error(f"Error building framing preview for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error building framing preview."}), 500


@workshop_bp.route('/<int:workshop_id>/presentation/rebuild', methods=['POST'])
@login_required
def presentation_rebuild(workshop_id: int) -> ResponseReturnValue:
    """Rebuild shortlist/action-plan artifacts in-phase with updated knobs.

    Body JSON: { mode?: 'shortlisting'|'action_plan'|'slideshow', weights?: {votes,feasibility,objective_fit}, constraints?: {max_items?} }

    Returns 200 JSON: { success: true, artifacts: {...} } where artifacts matches rebuild_presentation_artifacts output.
    """
    ws = Workshop.query.get_or_404(workshop_id)
    # Only organizer can tweak in-phase knobs
    user = cast(User, current_user)
    if not is_organizer(ws, user) and not current_app.config.get('LOGIN_DISABLED'):
        return jsonify({"success": False, "message": "Only the organizer can rebuild presentation artifacts."}), 403
    try:
        data = request.get_json(silent=True) or {}
        mode = data.get('mode')
        weights = data.get('weights') if isinstance(data.get('weights'), dict) else None
        constraints = data.get('constraints') if isinstance(data.get('constraints'), dict) else None
        # Coerce floats for weights safely
        if isinstance(weights, dict):
            w = {}
            for k in ('votes','feasibility','objective_fit'):
                if k in weights and weights[k] not in (None, ''):
                    try:
                        w[k] = float(weights[k])
                    except Exception:
                        pass
            weights = w or None
        # Clamp constraints
        if isinstance(constraints, dict):
            c = {}
            if constraints.get('max_items') not in (None, ''):
                try:
                    _mi_val = constraints.get('max_items')
                    c['max_items'] = max(1, int(str(_mi_val)))
                except Exception:
                    pass
            constraints = c or None

        artifacts = rebuild_presentation_artifacts(workshop_id, mode=mode, weights=weights, constraints=constraints)
        if isinstance(artifacts, dict) and artifacts.get('error'):
            return jsonify({"success": False, "message": artifacts.get('error')}), 400
        return jsonify({"success": True, "artifacts": artifacts})
    except Exception as e:
        current_app.logger.error(f"Error rebuilding presentation artifacts for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error rebuilding artifacts."}), 500


@workshop_bp.route('/<int:workshop_id>/participants/add', methods=['POST'])
@login_required
def add_participant(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if workshop.created_by_id != current_user.user_id:
        flash('Only the organizer can add participants.', 'danger')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))

    user_id = request.form.get('user_id', type=int)
    if not user_id:
        flash('Select a user to add.', 'warning')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))

    # Ensure selected user is a member of the workspace
    member = WorkspaceMember.query.filter_by(workspace_id=workshop.workspace_id, user_id=user_id, status='active').first()
    if not member:
        flash('User is not an active member of the workspace.', 'danger')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))

    existing = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, user_id=user_id).first()
    if existing:
        flash('User is already invited or participating.', 'info')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))

    try:
        p = WorkshopParticipant()
        p.workshop_id = workshop_id
        p.user_id = user_id
        p.role = 'participant'
        p.status = 'invited'
        p.generate_token()
        db.session.add(p)
        db.session.commit()
        flash('Invitation added.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error adding participant to workshop {workshop_id}: {e}')
        flash('Error adding participant.', 'danger')
    return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))


@workshop_bp.route('/<int:workshop_id>/participants/<int:participant_id>/remove', methods=['POST'])
@login_required
def remove_participant(workshop_id, participant_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if workshop.created_by_id != current_user.user_id:
        flash('Only the organizer can remove participants.', 'danger')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))
    p = WorkshopParticipant.query.filter_by(id=participant_id, workshop_id=workshop_id).first_or_404()
    # Prevent removing organizer record if exists
    if p.role == 'organizer':
        flash('Cannot remove the organizer.', 'warning')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))
    try:
        db.session.delete(p)
        db.session.commit()
        flash('Participant removed.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error removing participant {participant_id} from workshop {workshop_id}: {e}')
        flash('Error removing participant.', 'danger')
    return redirect(url_for('workshop_bp.view_workshop', workshop_id=workshop_id))


@workshop_bp.route('/invitation/<token>')
@login_required
def respond_invitation(token):
    """Accept or decline a workshop invitation using token and action query param."""
    participant_record = WorkshopParticipant.query.filter_by(invitation_token=token).first_or_404()
    if not participant_record.is_token_valid():
        flash('Invitation is invalid or expired.', 'warning')
        return redirect(url_for('account_bp.account'))

    action = request.args.get('action')
    if action == 'accept':
        participant_record.status = 'accepted'
        participant_record.joined_timestamp = datetime.utcnow()
        participant_record.invitation_token = None
        participant_record.token_expires = None
        db.session.commit()
        flash('Invitation accepted. You have joined the workshop.', 'success')
        return redirect(url_for('workshop_bp.view_workshop', workshop_id=participant_record.workshop_id))
    elif action == 'decline':
        participant_record.status = 'declined'
        participant_record.invitation_token = None
        participant_record.token_expires = None
        db.session.commit()
        flash('Invitation declined.', 'info')
        return redirect(url_for('account_bp.account'))

    # If no action, render a simple confirmation page
    return render_template('respond_invitation.html', participant_record=participant_record)

@workshop_bp.route("/<int:workshop_id>/next_task", methods=["POST"])
@login_required
def next_task(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        abort(403)

    if workshop.status != "inprogress":
        return jsonify({"error": "Workshop is not in progress."}), 400

    # Delegate to single orchestrator
    ok, payload_or_error = advance_to_next_task(workshop_id)
    if not ok:
        # End of sequence? Mark completed gracefully
        if str(payload_or_error).lower().startswith("no more tasks"):
            try:
                workshop.status = 'completed'
                workshop.current_task_id = None
                workshop.timer_start_time = None
                workshop.timer_paused_at = None
                workshop.timer_elapsed_before_pause = 0
                db.session.commit()
                emit_workshop_stopped(f"workshop_room_{workshop_id}", workshop_id)
                return jsonify({
                    "success": True,
                    "completed": True,
                    "redirect_url": url_for("workshop_bp.workshop_report", workshop_id=workshop_id)
                }), 200
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"Error completing workshop {workshop_id} at end of sequence: {e}")
                return jsonify({"error": "Failed to complete workshop."}), 500
        return jsonify({"error": payload_or_error}), 400

    return jsonify({"success": True, "task": payload_or_error})
@workshop_bp.route('/<int:workshop_id>/end_current', methods=['POST'])
@login_required
def end_current_task(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        abort(403)
    if workshop.current_task_id:
        task = db.session.get(BrainstormTask, workshop.current_task_id)
        if task and task.status == 'running':
            task.status = 'completed'
            task.ended_at = datetime.utcnow()
    # Keep index where it is; the UI can then call next/prev navigation
    try:
        db.session.commit()
        socketio.emit('task_completed', { 'workshop_id': workshop_id }, to=f'workshop_room_{workshop_id}')
        return jsonify({ 'success': True })
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error ending current task for workshop {workshop_id}: {e}")
        return jsonify({ 'success': False, 'message': 'Failed to end task' }), 500


@workshop_bp.route('/<int:workshop_id>/goto/<int:target_index>', methods=['POST'])
@login_required
def goto_task(workshop_id, target_index):
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        abort(403)
    ok, payload_or_error = go_to_task(workshop_id, target_index)
    if not ok:
        return jsonify({ 'success': False, 'message': str(payload_or_error) }), 400
    return jsonify({ 'success': True, 'task': payload_or_error })


@workshop_bp.route('/<int:workshop_id>/nav/prev', methods=['POST'])
@login_required
def nav_prev(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        abort(403)
    current_index = workshop.current_task_index if workshop.current_task_index is not None else -1
    # If we're before or at the first actionable index, disallow going back further
    if current_index <= 0:
        return jsonify({ 'success': False, 'message': 'Already at the first phase.' }), 400
    target_index = current_index - 1
    ok, payload_or_error = go_to_task(workshop_id, target_index)
    if not ok:
        return jsonify({ 'success': False, 'message': str(payload_or_error) }), 400
    return jsonify({ 'success': True, 'task': payload_or_error })


@workshop_bp.route('/<int:workshop_id>/nav/next', methods=['POST'])
@login_required
def nav_next(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        abort(403)
    current_index = workshop.current_task_index if workshop.current_task_index is not None else -1
    target_index = current_index + 1
    ok, payload_or_error = go_to_task(workshop_id, target_index)
    if not ok:
        return jsonify({ 'success': False, 'message': str(payload_or_error) }), 400
    return jsonify({ 'success': True, 'task': payload_or_error })
# Legacy next_task block removed; handled by orchestrator.
# (legacy next_task block removed; use orchestrator above)
# (legacy next_task block removed; advancement handled by orchestrator above)

# ---- WORKSHOP TASK MANAGEMENT ----

# --- Helper to get user's workspaces ---
def get_user_active_workspaces(user_id: int) -> list[Workspace]:
    """
    Returns a list of Workspace objects the user is an active member of.
    """
    return (
        Workspace.query.join(WorkspaceMember)
        .filter(WorkspaceMember.user_id == user_id, WorkspaceMember.status == "active")
        .order_by(Workspace.name)
        .all()
    )


def get_reference_documents_for_user(user_id: int, workspace_id: int | None = None) -> list[dict[str, Any]]:
    """Return lightweight metadata for documents the user can attach during creation."""
    try:
        query = Document.query

        if workspace_id:
            query = query.filter(Document.workspace_id == workspace_id)
        else:
            workspace_ids = {ws.workspace_id for ws in get_user_active_workspaces(user_id)}
            owned_ids = {
                ws.workspace_id
                for ws in Workspace.query.filter(Workspace.owner_id == user_id).all()
            }
            workspace_ids.update(owned_ids)
            if not workspace_ids:
                return []
            query = query.filter(Document.workspace_id.in_(workspace_ids))

        archived_attr = getattr(Document, "is_archived", None)
        if archived_attr is not None:
            archive_column = cast(Any, archived_attr)
            query = query.filter(or_(archive_column == False, archive_column.is_(None)))  # noqa: E712

        documents = query.order_by(Document.title.asc()).all()

        payload: list[dict[str, Any]] = []
        for doc in documents:
            workspace_name = (
                doc.workspace.name if getattr(doc, "workspace", None) else f"Workspace {doc.workspace_id}"
            )
            payload.append(
                {
                    "id": doc.id,
                    "title": doc.title,
                    "workspace_id": doc.workspace_id,
                    "workspace_name": workspace_name,
                    "description": doc.description or "",
                    "file_name": doc.file_name,
                }
            )
        return payload
    except Exception as exc:  # pragma: no cover - defensive guard for template rendering
        current_app.logger.error("Error loading reference documents: %s", exc)
        return []


def _process_reference_uploads(
    files: Sequence[FileStorage],
    *,
    workspace_id: int,
    user_id: int,
) -> tuple[list[int], list[str]]:
    """Persist uploaded reference documents prior to workshop creation.

    Returns (document_ids, warnings).
    """
    stored_ids: list[int] = []
    warnings: list[str] = []

    if not files:
        return stored_ids, warnings

    max_upload_bytes = getattr(Config, "MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
    tmp_root = Path(current_app.instance_path) / "uploads" / "tmp" / "workshop"
    final_root = Path(current_app.instance_path) / "uploads" / "documents"
    tmp_root.mkdir(parents=True, exist_ok=True)
    final_root.mkdir(parents=True, exist_ok=True)

    max_files = 10
    exceeded_limit = False
    for index, storage in enumerate(files):
        if index >= max_files:
            exceeded_limit = True
            break
        if not storage or not getattr(storage, "filename", None):
            continue

        original_name = storage.filename or ""
        safe_name = secure_filename(original_name)
        if not safe_name:
            warnings.append("Skipped a file with an invalid filename.")
            continue

        tmp_path: Path | None = None
        final_abs: Path | None = None
        try:
            # Enforce upload limit when metadata is available.
            content_length = getattr(storage, "content_length", None)
            if content_length and max_upload_bytes and content_length > max_upload_bytes:
                warnings.append(f"{original_name} exceeds the upload size limit.")
                continue

            token = uuid4().hex
            tmp_name = f"{token}_{safe_name}"
            tmp_path = tmp_root / tmp_name
            storage.save(tmp_path)

            final_name = tmp_name
            final_rel = os.path.join("uploads", "documents", final_name)
            final_abs = final_root / final_name
            shutil.move(tmp_path, final_abs)

            file_size = final_abs.stat().st_size
            title = Path(safe_name).stem or "Workshop Reference"

            document = Document()
            document.workspace_id = workspace_id
            document.uploaded_by_id = user_id
            document.title = title
            document.file_name = safe_name
            document.file_path = final_rel
            document.file_size = file_size
            document.description = "Uploaded during workshop creation"
            db.session.add(document)
            db.session.flush()

            doc_id = int(document.id)
            try:
                run_pipeline(doc_id)
            except Exception as exc:
                current_app.logger.error(
                    "Workshop reference upload failed to process",
                    exc_info=True,
                    extra={"document_id": doc_id, "filename": safe_name},
                )
                warnings.append(f"Failed to process {original_name}. It was not linked.")
                try:
                    failed = db.session.get(Document, doc_id)
                    if failed:
                        db.session.delete(failed)
                        db.session.commit()
                except Exception as cleanup_exc:  # pragma: no cover - defensive cleanup
                    current_app.logger.warning(
                        "Cleanup failed for draft document %s: %s", doc_id, cleanup_exc
                    )
                if final_abs is not None:
                    try:
                        final_abs.unlink(missing_ok=True)
                    except Exception:
                        pass
                continue

            stored_ids.append(doc_id)
        except Exception as exc:  # pragma: no cover - unexpected failure guard
            current_app.logger.exception("Unexpected error while handling reference upload: %s", exc)
            warnings.append(f"Unexpected error while processing {original_name}.")
            if final_abs is not None:
                try:
                    final_abs.unlink(missing_ok=True)
                except Exception:
                    pass
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    if exceeded_limit:
        warnings.append("Only the first 10 uploaded files were processed.")

    return stored_ids, warnings


def attach_documents_to_workshop(workshop: Workshop, document_ids: Sequence[Any]) -> None:
    """Persist links between a workshop and selected reference documents."""
    if not workshop or not document_ids:
        return

    parsed_ids: set[int] = set()
    for raw in document_ids:
        try:
            if raw is None:
                continue
            if isinstance(raw, int):
                parsed_ids.add(raw)
                continue
            text_value = str(raw).strip()
            if not text_value:
                continue
            parsed_ids.add(int(text_value))
        except (ValueError, TypeError):
            continue

    if not parsed_ids:
        return

    candidate_docs = (
        Document.query
        .filter(
            Document.id.in_(parsed_ids),
            Document.workspace_id == workshop.workspace_id,
        )
        .all()
    )
    if not candidate_docs:
        return

    valid_docs: list[Document] = []
    for doc in candidate_docs:
        is_archived = bool(getattr(doc, "is_archived", False))
        if is_archived:
            continue
        valid_docs.append(doc)

    if not valid_docs:
        return

    existing_links = {
        link.document_id
        for link in WorkshopDocument.query.filter(
            WorkshopDocument.workshop_id == workshop.id,
            WorkshopDocument.document_id.in_([doc.id for doc in valid_docs]),
        ).all()
    }

    for doc in valid_docs:
        if doc.id in existing_links:
            continue
        link = WorkshopDocument()
        link.workshop_id = workshop.id
        link.document_id = doc.id
        db.session.add(link)

    db.session.flush()

# --- Helper to determine if current user is the organizer ---
def is_organizer(workshop: Workshop, user: User | Any) -> bool:
    try:
        return bool(
            user
            and getattr(user, "is_authenticated", False)
            and workshop
            and workshop.created_by_id == user.user_id
        )
    except Exception:
        return False


@workshop_bp.route('/<int:workshop_id>/transcripts', methods=['GET'])
@login_required
def get_workshop_transcripts(workshop_id):
    ws = Workshop.query.get_or_404(workshop_id)
    is_org = (ws.created_by_id == current_user.user_id)
    participant = WorkshopParticipant.query.filter_by(workshop_id=ws.id, user_id=current_user.user_id).first()
    workspace_membership = WorkspaceMember.query.filter_by(workspace_id=ws.workspace_id, user_id=current_user.user_id, status='active').first()
    if not (is_org or participant or workspace_membership):
        return jsonify({"error": "Forbidden"}), 403
    # Return only persisted Transcript rows. entry_type distinguishes human vs facilitator.
    try:
        from app.models import Transcript as TranscriptModel, User as UserModel
        q = (
            db.session.query(TranscriptModel, UserModel.first_name, UserModel.last_name)
            .outerjoin(UserModel, TranscriptModel.user_id == UserModel.user_id)
            .filter(TranscriptModel.workshop_id == ws.id)
            .order_by(TranscriptModel.created_timestamp.asc())
        )
        rows = []
        for t, first_name, last_name in q.all():
            rows.append({
                'transcript_id': t.transcript_id,
                'workshop_id': t.workshop_id,
                'user_id': t.user_id,
                'first_name': first_name,
                'last_name': last_name,
                'entry_type': getattr(t, 'entry_type', 'human'),
                'raw_text': t.raw_stt_transcript or t.processed_transcript or '',
                'processed_text': t.processed_transcript or '',
                'language': t.language,
                'start_timestamp': t.start_timestamp.isoformat() if t.start_timestamp else None,
                'end_timestamp': t.end_timestamp.isoformat() if t.end_timestamp else None,
                'created_timestamp': t.created_timestamp.isoformat() if t.created_timestamp else None,
                'was_polished': bool((t.processed_transcript or '').strip() and (t.processed_transcript or '') != (t.raw_stt_transcript or '')),
            })
        return jsonify({"transcripts": rows, "count": len(rows)})
    except Exception as e:
        current_app.logger.error(f"Error loading transcripts for workshop {workshop_id}: {e}")
        return jsonify({"transcripts": [], "count": 0})


@workshop_bp.route('/<int:workshop_id>/chat', methods=['GET'])
@login_required
def get_chat_messages(workshop_id):
    """Return recent chat messages for this workshop filtered by scope.
    Query param: scope = 'workshop_chat' | 'discussion_chat' (default 'workshop_chat')
    """
    ws = Workshop.query.get_or_404(workshop_id)
    is_org = (ws.created_by_id == current_user.user_id)
    participant = WorkshopParticipant.query.filter_by(workshop_id=ws.id, user_id=current_user.user_id).first()
    workspace_membership = WorkspaceMember.query.filter_by(workspace_id=ws.workspace_id, user_id=current_user.user_id, status='active').first()
    if not (is_org or participant or workspace_membership):
        return jsonify({"error": "Forbidden"}), 403
    scope = (request.args.get('scope') or 'workshop_chat').strip()
    if scope not in ('workshop_chat', 'discussion_chat'):
        scope = 'workshop_chat'
    try:
        q = ChatMessage.query.filter_by(workshop_id=ws.id)
        try:
            q = q.filter(ChatMessage.chat_scope == scope)  # type: ignore[attr-defined]
        except Exception:
            pass
        rows = q.order_by(ChatMessage.timestamp.desc()).limit(100).all()
        rows.reverse()
        out = []
        for m in rows:
            try:
                mtype = getattr(m, 'message_type', 'user')
            except Exception:
                mtype = 'user'
            out.append({
                'id': m.id,
                'user_id': m.user_id,
                'user_name': m.username,
                'message': m.message,
                'timestamp': m.timestamp.isoformat() if m.timestamp else None,
                'message_type': mtype,
                'chat_scope': getattr(m, 'chat_scope', 'workshop_chat')
            })
        return jsonify({'messages': out, 'count': len(out), 'scope': scope})
    except Exception as e:
        current_app.logger.error(f"Error loading chat messages for workshop {workshop_id}: {e}")
        return jsonify({'messages': [], 'count': 0, 'scope': scope})


@workshop_bp.route('/<int:workshop_id>/settings/voting', methods=['POST'])
@login_required
def update_voting_settings(workshop_id):
    """Update voting-related settings like dots_per_user."""
    ws = Workshop.query.get_or_404(workshop_id)
    # Only organizer can change settings
    if not is_organizer(ws, current_user):
        return jsonify({"success": False, "message": "Forbidden"}), 403
    try:
        data = request.get_json(silent=True) or {}
        dots = data.get('dots_per_user')
        if dots is not None:
            try:
                dots = int(dots)
                if dots < 0 or dots > 1000:
                    raise ValueError('dots_per_user out of range')
            except Exception:
                return jsonify({"success": False, "message": "Invalid dots_per_user"}), 400
            ws.dots_per_user = dots
        db.session.commit()
        return jsonify({"success": True, "dots_per_user": ws.dots_per_user})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating voting settings for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error"}), 500


# --- 8. Add Document Link ---
@workshop_bp.route("/<int:workshop_id>/add_document", methods=["POST"])
@login_required
def add_document_link(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        flash("Only the workshop organizer can add documents.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    document_id_to_add = request.form.get("document_id", type=int)
    if not document_id_to_add:
        flash("No document selected to add.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Verify the document exists and belongs to the same workspace
    document_to_add = Document.query.filter_by(
        id=document_id_to_add, workspace_id=workshop.workspace_id
    ).first()

    if not document_to_add:
        flash(
            "Selected document is not valid or does not belong to this workspace.",
            "danger",
        )
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Check if already linked
    existing_link = WorkshopDocument.query.filter_by(
        workshop_id=workshop_id, document_id=document_id_to_add
    ).first()
    if existing_link:
        flash(
            f"Document '{document_to_add.title}' is already linked to this workshop.",
            "warning",
        )
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    try:
        new_link = WorkshopDocument()
        new_link.workshop_id = workshop_id
        new_link.document_id = document_id_to_add
        db.session.add(new_link)
        db.session.commit()
        flash(f"Document '{document_to_add.title}' linked successfully.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Could not link document due to a database conflict.", "warning")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error linking document {document_id_to_add} to workshop {workshop_id}: {e}"
        )
        flash("An error occurred while linking the document.", "danger")

    return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))


# --- 9. Remove Document Link ---
@workshop_bp.route("/<int:workshop_id>/remove_document/<int:link_id>", methods=["POST"])
@login_required
def remove_document_link(workshop_id, link_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    link_to_remove = WorkshopDocument.query.get_or_404(link_id)

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        flash("Only the workshop organizer can remove documents.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Ensure the link belongs to the correct workshop
    if link_to_remove.workshop_id != workshop_id:
        abort(404)

    try:
        doc_title = link_to_remove.document.title  # Get title before deleting
        db.session.delete(link_to_remove)
        db.session.commit()
        flash(f"Document link for '{doc_title}' removed successfully.", "success")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error removing document link {link_id} from workshop {workshop_id}: {e}"
        )
        flash("An error occurred while removing the document link.", "danger")

    return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))


# ################################
# Workshop Lifecycle Routes (Lobby, Room, Report)
# ################################


@workshop_bp.route("/join/<int:workshop_id>")
@login_required
def join_workshop(workshop_id):
    """
    Handles a user clicking the 'Join' button.
    Redirects to lobby if not started, room if in progress.
    """
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    # Basic permission check: Must be accepted participant (or organizer)
    if not participant:
        flash("You are not a participant in this workshop. Request participation first.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))
    if participant.role != 'organizer' and participant.status != 'accepted':
        if participant.status == 'requested':
            flash("Your participation request is pending organizer approval.", "info")
        elif participant.status == 'invited':
            flash("Please accept your invitation before joining.", "info")
        else:
            flash("You do not have permission to join this workshop.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Check workspace membership (optional but good practice)
    is_member = current_user.workspace_memberships.filter_by(
        workspace_id=workshop.workspace_id, status="active"
    ).first()
    if not is_member:
        flash("You must be an active member of the workspace to join.", "danger")
        return redirect(
            url_for("workspace_bp.view_workspace", workspace_id=workshop.workspace_id)
        )

    # Redirect based on status
    if workshop.status == "scheduled":
        # Mark participant status if needed (e.g., 'joined_lobby') - Optional
        # participant.status = 'joined_lobby' # Example
        # db.session.commit()
        return redirect(url_for("workshop_bp.workshop_lobby", workshop_id=workshop_id))
    elif workshop.status == "inprogress":
        # Mark participant status if needed (e.g., 'in_room') - Optional
        # participant.status = 'in_room' # Example
        # db.session.commit()
        return redirect(url_for("workshop_bp.workshop_room", workshop_id=workshop_id))
    elif workshop.status == "completed":
        flash("This workshop has already been completed.", "info")
        return redirect(url_for("workshop_bp.workshop_report", workshop_id=workshop_id))
    elif workshop.status == "cancelled":
        flash("This workshop has been cancelled.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))
    elif workshop.status == "paused":
        return redirect(url_for("workshop_bp.workshop_room", workshop_id=workshop_id))
    else:
        # Handle other statuses if necessary
        flash(
            f"Workshop is currently in status: {workshop.status}. Cannot join at this time.",
            "warning",
        )
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))







@workshop_bp.route("/lobby/<int:workshop_id>")
@login_required
def workshop_lobby(workshop_id):
    """Displays the waiting lobby for a scheduled workshop with AI content slots."""
    # Load workshop with eager relationships
    workshop = Workshop.query.get_or_404(workshop_id)
    
    # Now load participants (with their User) via a normal query:
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()
    
    # Add profile picture URL to each participant
    for participant in participants:
        participant.profile_pic_url = url_for('static', filename='images/default-profile.png')

    # And load linked documents (with their Document) explicitly:
    linked_docs = WorkshopDocument.query.filter_by(workshop_id=workshop.id).all()

    # Check if the user is a participant using the preloaded data
    participant = next((p for p in workshop.participants if p.user_id == current_user.user_id), None)

    # Permission checks
    if not participant:
        flash("You are not a participant in this workshop.", "danger")
        return redirect(url_for("workshop_bp.list_workshops"))
    

    # Status checks and redirects
    if workshop.status == "inprogress":
        flash("Workshop already in progress. Joining room...", "info")
        return redirect(url_for("workshop_bp.workshop_room", workshop_id=workshop_id))
    elif workshop.status == "completed":
        flash("Workshop completed. Viewing report...", "info")
        return redirect(url_for("workshop_bp.workshop_report", workshop_id=workshop_id))
    elif workshop.status != "scheduled":
        flash(f"Workshop status is '{workshop.status}'. Cannot access lobby.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # --- AI Content: Load or Generate ---
    save_needed = False
    ai_rules_raw = None
    ai_icebreaker_raw = None
    ai_tip_raw = None
    ai_agenda_raw = None 

    # Agenda (Load or Generate)
    if workshop.agenda: # Check the existing agenda field first
        ai_agenda_raw = workshop.agenda
        current_app.logger.debug(f"Loaded agenda from DB for workshop {workshop_id}")
    else:
        current_app.logger.debug(f"Generating agenda for workshop {workshop_id}")
        ai_agenda_raw = generate_agenda_text(workshop_id) # Generate if missing
        if ai_agenda_raw and not ai_agenda_raw.startswith("Could not generate"):
            workshop.agenda = ai_agenda_raw # Save to the standard agenda field
            save_needed = True
        else:
            ai_agenda_raw = "Could not generate an agenda at this time." # Fallback
            current_app.logger.warning(f"Failed to generate agenda for workshop {workshop_id}")


    # Rules
    if workshop.rules:
        ai_rules_raw = workshop.rules
        current_app.logger.debug(f"Loaded rules from DB for workshop {workshop_id}")
    else:
        current_app.logger.debug(f"Generating rules for workshop {workshop_id}")
        ai_rules_raw = generate_rules_text(workshop_id) # Generate if missing
        # Basic check for generation success (adjust if your function returns specific errors)
        if isinstance(ai_rules_raw, str) and not ai_rules_raw.startswith("Could not generate"):
            workshop.rules = ai_rules_raw
            save_needed = True
        else:
            ai_rules_raw = "Could not generate rules at this time."  # Provide fallback text
            current_app.logger.warning(f"Failed to generate rules for workshop {workshop_id}")

    # Icebreaker
    if workshop.icebreaker:
        ai_icebreaker_raw = workshop.icebreaker
        current_app.logger.debug(f"Loaded icebreaker from DB for workshop {workshop_id}")
    else:
        current_app.logger.debug(f"Generating icebreaker for workshop {workshop_id}")
        ai_icebreaker_raw = generate_icebreaker_text(workshop_id) # Generate if missing
        if ai_icebreaker_raw and not ai_icebreaker_raw.startswith("Could not generate"):
            workshop.icebreaker = ai_icebreaker_raw
            save_needed = True
        else:
            ai_icebreaker_raw = "Could not generate an icebreaker." # Fallback
            current_app.logger.warning(f"Failed to generate icebreaker for workshop {workshop_id}")

    # Tip (load or generate)
    if workshop.tip:
        ai_tip_raw = workshop.tip
        current_app.logger.debug(f"Loaded tip from DB for workshop {workshop_id}")
    else:
        current_app.logger.debug(f"Generating tip for workshop {workshop_id}")
        
        # Adjust check based on actual error/fallback message from generate_tip_text
        ai_tip_raw = generate_tip_text(workshop_id)
        if isinstance(ai_tip_raw, str) and not ai_tip_raw.startswith("No pre‑workshop data found") and not ai_tip_raw.startswith("Could not generate"):
            workshop.tip = ai_tip_raw
            save_needed = True
        else:
            ai_tip_raw = "Could not generate a tip." # Fallback
            current_app.logger.warning(f"Failed to generate tip for workshop {workshop_id}")

    # Save to DB if any content was newly generated
    if save_needed:
        try:
            db.session.commit()
            current_app.logger.info(f"Saved newly generated AI content for workshop {workshop_id}")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error saving generated AI content for workshop {workshop_id}: {e}")
            # Don't necessarily fail the request, but log the error
            flash("Could not save generated content. Please try refreshing.", "warning")

    # Convert raw text to Markdown HTML
    # Use the standard markdown filter for consistency
    ai_agenda_html = markdown.markdown(ai_agenda_raw or "No agenda available.") if isinstance(ai_agenda_raw, str) else markdown.markdown("No agenda available.")
    ai_rules_html = markdown.markdown(ai_rules_raw or "No rules available.") if isinstance(ai_rules_raw, str) else markdown.markdown("No rules available.")
    ai_icebreaker_html = markdown.markdown(ai_icebreaker_raw or "No icebreaker available.") if isinstance(ai_icebreaker_raw, str) else markdown.markdown("No icebreaker available.")
    ai_tip_html = markdown.markdown(ai_tip_raw or "No tip available.") if isinstance(ai_tip_raw, str) else markdown.markdown("No tip available.")

    agenda_items_sorted = sorted(list(workshop.agenda_items), key=lambda item: (item.position or 0, item.id)) if hasattr(workshop, "agenda_items") else []
    lobby_tts_content = {
        "agenda": _compute_tts_for_lobby("agenda", raw=ai_agenda_raw if isinstance(ai_agenda_raw, str) else None, html_text=ai_agenda_html, items=agenda_items_sorted),
        "rules": _compute_tts_for_lobby("rules", raw=ai_rules_raw if isinstance(ai_rules_raw, str) else None, html_text=ai_rules_html),
        "icebreaker": _compute_tts_for_lobby("icebreaker", raw=ai_icebreaker_raw if isinstance(ai_icebreaker_raw, str) else None, html_text=ai_icebreaker_html),
        "tip": _compute_tts_for_lobby("tip", raw=ai_tip_raw if isinstance(ai_tip_raw, str) else None, html_text=ai_tip_html),
    }


    # Get participants list for display
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()

    # Add profile picture URL to each participant
    for participant in participants:
        participant.profile_pic_url = url_for('static', filename='images/default-profile.png')
        
    # Get linked documents (already loaded via joinedload on workshop query)
    # Linked documents: convert dynamic relationship (query) to list for iteration
    linked_docs = workshop.linked_documents.all()
    
    # Check if current user is the organizer
    is_organizer_flag = workshop.created_by_id == current_user.user_id
    

    # Debugging print statement (optional)
    # print(f"Passing to template - Rules HTML: {ai_rules_html}")

    return render_template(
        "workshop_lobby.html",
        workshop=workshop,
        participants=participants,
        current_participant=participant,
        linked_documents=linked_docs,
        documents=[ld.document for ld in linked_docs],  # For lobby template JSON hydration
        ai_agenda=ai_agenda_html,
        ai_rules=ai_rules_html,
        ai_icebreaker=ai_icebreaker_html,
        ai_tip=ai_tip_html,
        user_is_organizer=is_organizer_flag,
        lobby_tts_content=lobby_tts_content,
    )
    
    



















# --- Add New Routes for Regenerating and Editing AI Content ---

# Helper function for permission check
def check_organizer_permission(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if workshop.created_by_id != current_user.user_id:
        abort(403, description="You do not have permission to perform this action.")
    return workshop

@workshop_bp.route("/<int:workshop_id>/regenerate/rules", methods=["POST"])
@login_required
def regenerate_rules(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    try:
        new_rules_raw = generate_rules_text(workshop_id)
        if isinstance(new_rules_raw, str) and not new_rules_raw.startswith("Could not generate"):
            workshop.rules = new_rules_raw
            db.session.commit()
            new_rules_html = markdown.markdown(new_rules_raw)
            tts_text = _compute_tts_for_lobby('rules', raw=new_rules_raw, html_text=new_rules_html)
            # Emit WebSocket event (optional but good for real-time updates)
            socketio.emit('ai_content_update', {
                'workshop_id': workshop_id,
                'type': 'rules',
                'content': new_rules_html,
                'tts_text': tts_text,
            }, to=f'workshop_lobby_{workshop_id}')
            return jsonify({"success": True, "content": new_rules_html, "tts_text": tts_text})
        else:
            return jsonify({"success": False, "message": "Failed to generate new rules."}), 500
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating rules for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error during regeneration."}), 500

@workshop_bp.route("/<int:workshop_id>/regenerate/icebreaker", methods=["POST"])
@login_required
def regenerate_icebreaker(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    try:
        new_icebreaker_raw = generate_icebreaker_text(workshop_id)
        if isinstance(new_icebreaker_raw, str) and not new_icebreaker_raw.startswith("Could not generate"):
            workshop.icebreaker = new_icebreaker_raw
            db.session.commit()
            new_icebreaker_html = markdown.markdown(new_icebreaker_raw)
            tts_text = _compute_tts_for_lobby('icebreaker', raw=new_icebreaker_raw, html_text=new_icebreaker_html)
            socketio.emit('ai_content_update', {
                'workshop_id': workshop_id,
                'type': 'icebreaker',
                'content': new_icebreaker_html,
                'tts_text': tts_text,
            }, to=f'workshop_lobby_{workshop_id}')
            return jsonify({"success": True, "content": new_icebreaker_html, "tts_text": tts_text})
        else:
            return jsonify({"success": False, "message": "Failed to generate new icebreaker."}), 500
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating icebreaker for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error during regeneration."}), 500

@workshop_bp.route("/<int:workshop_id>/regenerate/tip", methods=["POST"])
@login_required
def regenerate_tip(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    try:
        new_tip_raw = generate_tip_text(workshop_id)
        if isinstance(new_tip_raw, str) and not new_tip_raw.startswith("No pre‑workshop data found"):
            workshop.tip = new_tip_raw
            db.session.commit()
            new_tip_html = markdown.markdown(new_tip_raw)
            tts_text = _compute_tts_for_lobby('tip', raw=new_tip_raw, html_text=new_tip_html)
            socketio.emit('ai_content_update', {
                'workshop_id': workshop_id,
                'type': 'tip',
                'content': new_tip_html,
                'tts_text': tts_text,
            }, to=f'workshop_lobby_{workshop_id}')
            return jsonify({"success": True, "content": new_tip_html, "tts_text": tts_text})
        else:
            return jsonify({"success": False, "message": "Failed to generate new tip."}), 500
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating tip for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error during regeneration."}), 500


@workshop_bp.route("/<int:workshop_id>/edit/rules", methods=["POST"])
@login_required
def edit_rules(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    payload = request.get_json(silent=True) or {}
    edited_content = payload.get('content')
    if edited_content is None:
        return jsonify({"success": False, "message": "No content provided."}), 400
    try:
        workshop.rules = edited_content  # Store raw markdown/text
        db.session.commit()
        edited_content_html = markdown.markdown(edited_content)
        tts_text = _compute_tts_for_lobby('rules', raw=edited_content, html_text=edited_content_html)
        socketio.emit(
            'ai_content_update',
            {
                'workshop_id': workshop_id,
                'type': 'rules',
                'content': edited_content_html,
                'tts_text': tts_text,
            },
            to=f'workshop_lobby_{workshop_id}',
        )
        return jsonify({"success": True, "content": edited_content_html, "tts_text": tts_text})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving edited rules for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error saving edit."}), 500

@workshop_bp.route("/<int:workshop_id>/edit/icebreaker", methods=["POST"])
@login_required
def edit_icebreaker(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    payload = request.get_json(silent=True) or {}
    edited_content = payload.get('content')
    if edited_content is None:
        return jsonify({"success": False, "message": "No content provided."}), 400
    try:
        workshop.icebreaker = edited_content
        db.session.commit()
        edited_content_html = markdown.markdown(edited_content)
        tts_text = _compute_tts_for_lobby('icebreaker', raw=edited_content, html_text=edited_content_html)
        socketio.emit(
            'ai_content_update',
            {
                'workshop_id': workshop_id,
                'type': 'icebreaker',
                'content': edited_content_html,
                'tts_text': tts_text,
            },
            to=f'workshop_lobby_{workshop_id}',
        )
        return jsonify({"success": True, "content": edited_content_html, "tts_text": tts_text})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving edited icebreaker for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error saving edit."}), 500

@workshop_bp.route("/<int:workshop_id>/edit/tip", methods=["POST"])
@login_required
def edit_tip(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    payload = request.get_json(silent=True) or {}
    edited_content = payload.get('content')
    if edited_content is None:
        return jsonify({"success": False, "message": "No content provided."}), 400
    try:
        workshop.tip = edited_content
        db.session.commit()
        edited_content_html = markdown.markdown(edited_content)
        tts_text = _compute_tts_for_lobby('tip', raw=edited_content, html_text=edited_content_html)
        socketio.emit(
            'ai_content_update',
            {
                'workshop_id': workshop_id,
                'type': 'tip',
                'content': edited_content_html,
                'tts_text': tts_text,
            },
            to=f'workshop_lobby_{workshop_id}',
        )
        return jsonify({"success": True, "content": edited_content_html, "tts_text": tts_text})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving edited tip for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error saving edit."}), 500


@workshop_bp.route("/<int:workshop_id>/regenerate/agenda", methods=["POST"])
@login_required
def regenerate_agenda(workshop_id):
    """Regenerate agenda via LLM and replace normalized rows + cache JSON string in workshop.agenda.

    Fallback behavior: if the LLM returns plain text (lines) instead of valid JSON, we coerce it into
    the required JSON structure {"agenda": [{"activity":..., "description":..., "estimated_duration": null, "time_slot": null}, ...]}
    so the user still gets a usable agenda and we avoid returning a 500.
    """
    ws = check_organizer_permission(workshop_id)
    try:
        raw = generate_agenda_text(workshop_id)
        # Attempt to parse as JSON first
        parsed = None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
        elif isinstance(raw, dict):
            parsed = raw

        items = None
        if isinstance(parsed, dict) and isinstance(parsed.get('agenda'), list):
            items = parsed['agenda']
        elif isinstance(parsed, list):
            items = parsed
        # Fallback: treat raw string as newline or bullet separated agenda
        if items is None and isinstance(raw, str):
            # Split lines, strip bullets like '-', '*', numbering '1.' etc.
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            cleaned = []
            for ln in lines:
                cleaned.append(re.sub(r'^(?:[-*+]\s+|\d+\.|\d+\)|\u2022\s+)', '', ln).strip())
            cleaned = [c for c in cleaned if c and not c.lower().startswith('could not generate agenda')]
            if cleaned:
                items = [{
                    'time_slot': None,
                    'activity': c,
                    'description': c,
                    'estimated_duration': None,
                } for c in cleaned]
        if not isinstance(items, list):
            current_app.logger.error(f"LLM did not return valid JSON for agenda regen workshop {workshop_id}")
            return jsonify({"success": False, "message": "Invalid agenda JSON"}), 500
        # Persist normalized rows (ensure each item dict has activity key)
        norm = []
        for it in items:
            if isinstance(it, dict):
                activity = it.get('activity') or it.get('title') or it.get('name') or it.get('task')
                if not activity and len(it.keys()) == 1:
                    # Single key dict like {'Brainstorming Ideation (Timed)': '...' }
                    k = next(iter(it.keys()))
                    activity = k
                if not activity:
                    continue
                norm.append({
                    'time_slot': it.get('time_slot'),
                    'activity': activity,
                    'description': it.get('description') or it.get('details') or activity,
                    'estimated_duration': it.get('estimated_duration') or it.get('duration'),
                })
            elif isinstance(it, str):
                norm.append({
                    'time_slot': None,
                    'activity': it,
                    'description': it,
                    'estimated_duration': None,
                })
        if not norm:
            return jsonify({"success": False, "message": "No usable agenda items"}), 500
        _replace_agenda_rows(ws.id, norm, source='llm')
        compiled_json = json.dumps({'agenda': norm})
        tts_text = _compute_tts_for_lobby('agenda', items=norm)
        try:
            ws.agenda = compiled_json
            db.session.commit()
        except Exception:
            db.session.rollback()
        socketio.emit(
            "ai_content_update",
            {
                "workshop_id": workshop_id,
                "type": "agenda",
                "content": compiled_json,
                "tts_text": tts_text,
            },
            to=f"workshop_lobby_{workshop_id}",
        )
        return jsonify({"success": True, "count": len(norm), "content": compiled_json, "tts_text": tts_text}), 200
    except Exception as e:
        current_app.logger.error(f"Error regenerating agenda rows: {e}")
        return jsonify({"success": False, "message": "Failed to regenerate agenda"}), 500


# --- Render Workshop Room ---
@workshop_bp.route("/room/<int:workshop_id>")
@login_required
def workshop_room(workshop_id):
    """Displays the main workshop room."""
    workshop = Workshop.query.options(
    ).get_or_404(workshop_id)

    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    if not participant:
        flash("You are not a participant in this workshop.", "danger")
        return redirect(url_for("workshop_bp.list_workshops"))

    # Redirect based on status
    if workshop.status == "scheduled":
        return redirect(url_for("workshop_bp.workshop_lobby", workshop_id=workshop_id))
    elif workshop.status == "completed":
        return redirect(url_for("workshop_bp.workshop_report", workshop_id=workshop_id))
    elif workshop.status not in ["inprogress", "paused"]:
        flash(f"Workshop status is '{workshop.status}'. Cannot access room.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Provide lightweight participant payload for assistant features (action item creation)
    participants_payload: list[dict[str, Any]] = []
    try:
        participants = WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()
        for p in participants:
            user = getattr(p, "user", None)
            display_name = None
            if user is not None:
                display_name = getattr(user, "first_name", None) or getattr(user, "display_name", None)
                if not display_name:
                    email = getattr(user, "email", "")
                    display_name = email.split("@")[0] if email else None
            participants_payload.append(
                {
                    "id": p.id,
                    "user_id": p.user_id,
                    "display_name": display_name or "Participant",
                    "email": getattr(getattr(p, "user", None), "email", None),
                }
            )
    except Exception:
        participants_payload = []

    can_manage_actions = bool(
        participant
        and (
            getattr(participant, "role", "") == "organizer"
            or workshop.created_by_id == current_user.user_id
        )
    )

    return render_template(
        "workshop_room.html",
        workshop=workshop,
        # Pass minimal necessary data, JS handles the rest
        # participants=participants, # Removed, handled by sockets
        current_participant=participant, # Keep for user context
        participants_payload=participants_payload,
        can_manage_actions=can_manage_actions,
    )




# --- Action Items API (Organizer only for write operations) ---
@workshop_bp.route("/<int:workshop_id>/action_items", methods=["GET"]) 
@login_required
def list_action_items(workshop_id):
    ws = Workshop.query.get_or_404(workshop_id)
    # Any participant can view action items
    items = ActionItem.query.filter_by(workshop_id=ws.id).order_by(ActionItem.status.asc(), ActionItem.due_date.asc().nullslast(), ActionItem.created_at.asc()).all()
    def _ser(ai: ActionItem):
        return {
            "id": ai.id,
            "title": ai.title,
            "description": ai.description,
            "status": ai.status,
            "due_date": ai.due_date.isoformat() if ai.due_date else None,
            "owner_participant_id": ai.owner_participant_id,
            "task_id": ai.task_id,
        }
    return jsonify({"items": [_ser(i) for i in items]})


@workshop_bp.route("/<int:workshop_id>/action_items", methods=["POST"]) 
@login_required
def create_action_item(workshop_id):
    ws = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(ws, current_user):
        return jsonify({"error": "Permission denied"}), 403
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400
    try:
        ai = ActionItem()
        ai.workshop_id = ws.id
        # Validate task belongs to workshop
        task_id = data.get("task_id")
        if task_id:
            t = BrainstormTask.query.filter_by(id=task_id, workshop_id=ws.id).first()
            ai.task_id = t.id if t else None
        ai.title = title
        ai.description = (data.get("description") or None)
        # Validate owner participant belongs to workshop
        owner_pid = data.get("owner_participant_id")
        if owner_pid:
            op = WorkshopParticipant.query.filter_by(id=owner_pid, workshop_id=ws.id).first()
            ai.owner_participant_id = op.id if op else None
        # Parse due_date (YYYY-MM-DD)
        due_raw = data.get("due_date")
        if due_raw:
            try:
                ai.due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
            except Exception:
                pass
        ai.status = (data.get("status") or 'todo').strip().lower()[:50]
        db.session.add(ai)
        db.session.commit()
        return jsonify({"success": True, "id": ai.id}), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to create action item for workshop {workshop_id}: {e}")
        return jsonify({"error": "Failed to create action item"}), 500


@workshop_bp.route("/<int:workshop_id>/action_items/import", methods=["POST"]) 
@login_required
def import_action_items_endpoint(workshop_id):
    ws = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(ws, current_user):
        return jsonify({"error": "Permission denied"}), 403
    try:
        result, code = import_action_items(workshop_id)
        return jsonify(result), code
    except Exception as e:
        current_app.logger.error(f"Failed to import action items for workshop {workshop_id}: {e}")
        return jsonify({"error": "Server error importing action items"}), 500


@workshop_bp.route("/<int:workshop_id>/action_items/<int:item_id>", methods=["PATCH"]) 
@login_required
def update_action_item(workshop_id, item_id):
    ws = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(ws, current_user):
        return jsonify({"error": "Permission denied"}), 403
    ai = ActionItem.query.filter_by(id=item_id, workshop_id=ws.id).first_or_404()
    data = request.get_json(silent=True) or {}
    try:
        if "title" in data: ai.title = (data.get("title") or ai.title)
        if "description" in data: ai.description = (data.get("description") or None)
        if "status" in data: ai.status = (data.get("status") or ai.status)
        if "owner_participant_id" in data:
            owner_pid = data.get("owner_participant_id")
            if owner_pid:
                op = WorkshopParticipant.query.filter_by(id=owner_pid, workshop_id=ws.id).first()
                ai.owner_participant_id = op.id if op else None
            else:
                ai.owner_participant_id = None
        if "task_id" in data:
            task_id = data.get("task_id")
            if task_id:
                t = BrainstormTask.query.filter_by(id=task_id, workshop_id=ws.id).first()
                ai.task_id = t.id if t else None
            else:
                ai.task_id = None
        if "due_date" in data:
            due_raw = data.get("due_date")
            if due_raw:
                try:
                    ai.due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
                except Exception:
                    pass
            else:
                ai.due_date = None
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to update action item {item_id} for workshop {workshop_id}: {e}")
        return jsonify({"error": "Failed to update action item"}), 500


@workshop_bp.route("/<int:workshop_id>/action_items/<int:item_id>", methods=["DELETE"]) 
@login_required
def delete_action_item(workshop_id, item_id):
    ws = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(ws, current_user):
        return jsonify({"error": "Permission denied"}), 403
    ai = ActionItem.query.filter_by(id=item_id, workshop_id=ws.id).first_or_404()
    try:
        db.session.delete(ai)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to delete action item {item_id} for workshop {workshop_id}: {e}")
        return jsonify({"error": "Failed to delete action item"}), 500


# --- Settings: Auto-advance toggle ---
@workshop_bp.route("/<int:workshop_id>/settings/auto_advance", methods=["POST"])
@login_required
def update_auto_advance(workshop_id):
    """Organizer can enable/disable auto-advance and set delay seconds."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify(success=False, message="Permission denied"), 403

    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}

    enabled = bool(data.get("enabled", workshop.auto_advance_enabled))
    after_seconds_raw = data.get("after_seconds", workshop.auto_advance_after_seconds)
    try:
        after_seconds = max(0, int(after_seconds_raw)) if after_seconds_raw is not None else 0
    except (ValueError, TypeError):
        after_seconds = workshop.auto_advance_after_seconds or 0

    workshop.auto_advance_enabled = enabled
    workshop.auto_advance_after_seconds = after_seconds
    db.session.commit()

    # Broadcast update so all clients adjust immediately
    socketio.emit(
        "auto_advance_update",
        {"workshop_id": workshop_id, "enabled": enabled, "after_seconds": after_seconds},
        to=f"workshop_room_{workshop_id}"
    )
    return jsonify(success=True)

# --- Workshop Lifecycle Routes ---

@workshop_bp.route("/start/<int:workshop_id>", methods=["POST"])
@login_required
def start_workshop(workshop_id):
    """Starts the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user): # Use helper
        return jsonify({"success": False, "message": "Permission denied"}), 403

    if workshop.status != "scheduled":
        return jsonify({"success": False, "message": f"Workshop status is {workshop.status}"}), 400

    workshop.status = "inprogress"
    # Reset timer fields in case it was previously stopped/paused incorrectly
    workshop.current_task_id = None
    workshop.timer_start_time = None
    workshop.timer_paused_at = None
    workshop.timer_elapsed_before_pause = 0
    workshop.current_task_index = None # Reset task sequence index

    db.session.commit()

    socketio.emit(
        "workshop_started",
        {"workshop_id": workshop_id},
    to=f"workshop_lobby_{workshop_id}",
    )
    socketio.emit(
        "workshop_status_update",
        {"workshop_id": workshop_id, "status": "inprogress"},
    to=f"workshop_room_{workshop_id}",
    )


    flash("Workshop started successfully!", "success")
    return jsonify(
        success=True,
        message="Workshop started",
        redirect_url=url_for("workshop_bp.workshop_room", workshop_id=workshop_id),
    )

@workshop_bp.route("/pause/<int:workshop_id>", methods=["POST"])
@login_required
def pause_workshop(workshop_id):
    """Pauses the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify({"success": False, "message": "Permission denied"}), 403

    if workshop.status != "inprogress":
        return jsonify({"success": False, "message": f"Workshop status is {workshop.status}"}), 400

    workshop.status = "paused"
    if workshop.timer_start_time: # Only calculate elapsed time if a timer was running
        elapsed_this_run = (datetime.utcnow() - workshop.timer_start_time).total_seconds()
        workshop.timer_elapsed_before_pause += int(elapsed_this_run)
        workshop.timer_paused_at = datetime.utcnow()
        workshop.timer_start_time = None # Clear start time as it's now paused

    db.session.commit()

    emit_workshop_paused(f"workshop_room_{workshop_id}", workshop_id) # Use helper emitter

    flash("Workshop paused successfully.", "success")
    # No redirect needed if handled by socket event + JS reload
    return jsonify(success=True, message="Workshop paused")


@workshop_bp.route("/resume/<int:workshop_id>", methods=["POST"])
@login_required
def resume_workshop(workshop_id):
    """Resumes the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify({"success": False, "message": "Permission denied"}), 403

    if workshop.status != "paused":
        return jsonify({"success": False, "message": f"Workshop status is {workshop.status}"}), 400

    workshop.status = "inprogress"
    if workshop.current_task_id and workshop.timer_paused_at: # Only set start time if resuming a task timer
        workshop.timer_start_time = datetime.utcnow() # Set new start time for the current run
        workshop.timer_paused_at = None # Clear paused time

    db.session.commit()

    emit_workshop_resumed(f"workshop_room_{workshop_id}", workshop_id) # Use helper emitter

    flash("Workshop resumed successfully.", "success")
    # No redirect needed if handled by socket event + JS reload
    return jsonify(success=True, message="Workshop resumed")



@workshop_bp.route("/stop/<int:workshop_id>", methods=["POST"])
@login_required
def stop_workshop(workshop_id):
    """Stops the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify({"success": False, "message": "Permission denied"}), 403

    # Allow stopping from 'inprogress' or 'paused'
    if workshop.status not in ["inprogress", "paused"]:
        return jsonify({"success": False, "message": f"Workshop status is {workshop.status}"}), 400

    workshop.status = "completed"
    # Clear current task and timer state
    if workshop.current_task_id:
        task = db.session.get(BrainstormTask, workshop.current_task_id)
        if task and task.status == 'running':
            task.status = 'completed' # Mark task as completed
            task.ended_at = datetime.utcnow()
    workshop.current_task_id = None
    workshop.timer_start_time = None
    workshop.timer_paused_at = None
    workshop.timer_elapsed_before_pause = 0
    # workshop.current_task_index = None # Keep index if needed for report?
    clear_workshop_tracking(workshop_id) # Clear moderator tracking
    
    db.session.commit()

    emit_workshop_stopped(f"workshop_room_{workshop_id}", workshop_id) # Use helper emitter

    flash("Workshop stopped and completed.", "success")
    return jsonify(
        success=True,
        message="Workshop stopped",
        redirect_url=url_for("workshop_bp.workshop_report", workshop_id=workshop_id),
    )


@workshop_bp.route("/report/<int:workshop_id>")
@login_required
def workshop_report(workshop_id):
    """Displays the post-workshop report."""
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    # Permission checks
    if not participant:
        flash("You are not a participant in this workshop.", "danger")
        return redirect(url_for("workshop_bp.list_workshops"))

    if workshop.status != "completed":
        flash("Workshop report is only available after completion.", "warning")
        # Redirect based on current status
        if workshop.status == "scheduled":
            return redirect(
                url_for("workshop_bp.workshop_lobby", workshop_id=workshop_id)
            )
        elif workshop.status == "inprogress":
            return redirect(
                url_for("workshop_bp.workshop_room", workshop_id=workshop_id)
            )
        else:
            return redirect(
                url_for("workshop_bp.view_workshop", workshop_id=workshop_id)
            )

    # Get participants list
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()
    participants_payload = []
    try:
        for p in participants:
            u = getattr(p, 'user', None)
            participants_payload.append({
                'id': p.id,
                'user': {
                    'first_name': getattr(u, 'first_name', None) if u else None,
                    'email': getattr(u, 'email', None) if u else None,
                    'profile_pic_url': getattr(u, 'profile_pic_url', None) if u else None,
                }
            })
    except Exception:
        participants_payload = []
    can_edit = False
    try:
        can_edit = is_organizer(workshop, current_user)
    except Exception:
        can_edit = (participant and getattr(participant, 'role', '') == 'organizer') or (workshop.created_by_id == current_user.user_id)

    # --- Helper to load latest payload for given task types ---
    def _latest_task_payload(task_types: Sequence[str]) -> tuple[BrainstormTask | None, dict[str, Any]]:
        if not task_types:
            return None, {}
        try:
            task = (
                BrainstormTask.query
                .filter(BrainstormTask.workshop_id == workshop.id, BrainstormTask.task_type.in_(task_types))
                .order_by(BrainstormTask.created_at.desc())
                .first()
            )
            if not task:
                return None, {}
            raw_payload = task.payload_json or task.prompt or ""
            if not raw_payload:
                return task, {}
            try:
                data = json.loads(raw_payload)
                if isinstance(data, dict):
                    return task, data
            except Exception:
                current_app.logger.warning(
                    "[Report] Failed to parse payload for task %s (types=%s)",
                    getattr(task, "id", None), task_types,
                    exc_info=True,
                )
            return task, {}
        except Exception as exc:
            current_app.logger.error(
                "[Report] Payload lookup failed for workshop %s types=%s: %s",
                workshop.id,
                task_types,
                exc,
                exc_info=True,
            )
            return None, {}

    # --- Build report data (summary, action items, transcript, timeline) ---
    # Tasks (ordered)
    tasks = (
        BrainstormTask.query.filter_by(workshop_id=workshop.id)
        .order_by(BrainstormTask.started_at.asc())
        .all()
    )

    # Summary content from the 'summary' task payload, if present
    summary_html = None
    action_items = []
    summary_task, summary_payload = _latest_task_payload(["summary"])
    try:
        raw_summary = summary_payload.get('summary_report') or summary_payload.get('summary')
        if isinstance(raw_summary, str) and raw_summary.strip():
            try:
                summary_html = markdown.markdown(raw_summary)
            except Exception:
                summary_html = f"<pre class='mb-0 small'>{escape(raw_summary)}</pre>"
        items = summary_payload.get('action_items') or []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    action_items.append({
                        'action': it.get('action') or it.get('title') or '',
                        'owner': it.get('owner') or '',
                        'due': it.get('due') or '',
                        'status': it.get('status') or ''
                    })
                else:
                    action_items.append({'action': str(it), 'owner': '', 'due': '', 'status': ''})
    except Exception as e:
        current_app.logger.warning(f"Failed to build summary/action items for report {workshop_id}: {e}")

    # Transcript: basic chat log
    transcript = []
    try:
        chat_messages = (
            ChatMessage.query.filter_by(workshop_id=workshop.id)
            .order_by(ChatMessage.timestamp.asc())
            .all()
        )
        for m in chat_messages:
            transcript.append({
                'user': getattr(m, 'username', None) or 'User',
                'message': m.message,
                'timestamp': m.timestamp.isoformat() if getattr(m, 'timestamp', None) else '',
                'timestamp_display': m.timestamp.strftime('%Y-%m-%d %H:%M') if getattr(m, 'timestamp', None) else ''
            })
    except Exception as e:
        current_app.logger.warning(f"Failed to build transcript for report {workshop_id}: {e}")

    # Timeline: summarize tasks
    timeline = []
    total_ideas_count = 0
    total_clusters_count = 0
    try:
        for t in tasks:
            # Count ideas per task (if relationship available)
            try:
                idea_count = t.ideas.count() if hasattr(t, 'ideas') and hasattr(t.ideas, 'count') else (len(t.ideas) if hasattr(t, 'ideas') else 0)
            except Exception:
                idea_count = 0
            # Count clusters per task if relationship is present
            try:
                cluster_count = t.clusters.count() if hasattr(t, 'clusters') and hasattr(t.clusters, 'count') else (len(t.clusters) if hasattr(t, 'clusters') else 0)
            except Exception:
                cluster_count = 0
            total_ideas_count += idea_count
            total_clusters_count += cluster_count
            timeline.append({
                'task_id': t.id,
                'task_type': (t.task_type or '').upper(),
                'title': t.title or t.task_type or 'Task',
                'description': t.description or '',
                'duration': t.duration or 0,
                'status': t.status or '',
                'started_at': t.started_at.isoformat() if t.started_at else '',
                'ended_at': t.ended_at.isoformat() if t.ended_at else '',
                'ideas_count': idea_count,
                'clusters_count': cluster_count,
            })
    except Exception as e:
        current_app.logger.warning(f"Failed to build task timeline for report {workshop_id}: {e}")

    # --- Advanced report insights & artifacts ---
    canonical_session = {}
    summary_artifacts = []
    if summary_payload:
        maybe_canonical = summary_payload.get('canonical_session_json')
        if isinstance(maybe_canonical, str):
            try:
                maybe_canonical = json.loads(maybe_canonical)
            except Exception:
                maybe_canonical = {}
        if isinstance(maybe_canonical, dict):
            canonical_session = maybe_canonical

        def _artifact_entry(label: str, url_key: str, fmt: str, description: str) -> None:
            url_val = summary_payload.get(url_key)
            if isinstance(url_val, str) and url_val.strip():
                summary_artifacts.append({
                    'label': label,
                    'format': fmt,
                    'url': url_val,
                    'description': description,
                })

        _artifact_entry("Executive Brief (PDF)", "summary_pdf_url", "PDF", "Share-ready executive summary")
        _artifact_entry("Executive Brief (Slides)", "summary_pptx_url", "Slides", "Quick playback deck covering highlights")
        _artifact_entry("Executive Brief (Markdown)", "summary_markdown_url", "Markdown", "Raw markdown for downstream editing")

    # Feasibility / Prioritization / Action Plan payloads
    feasibility_task, feasibility_payload = _latest_task_payload(["results_feasibility", "feasibility"])
    prioritization_task, prioritization_payload = _latest_task_payload(["results_prioritization", "prioritization"])
    action_plan_task, action_plan_payload = _latest_task_payload(["results_action_plan", "action_plan"])

    report_artifacts: list[dict[str, Any]] = list(summary_artifacts)
    def _append_artifact(payload: dict[str, Any], label: str, url_keys: Sequence[str], fmt: str, description: str, badge: str | None = None) -> None:
        for key in url_keys:
            url_val = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(url_val, str) and url_val.strip():
                report_artifacts.append({
                    'label': label,
                    'format': fmt,
                    'url': url_val,
                    'description': description,
                    'badge': badge,
                })
                break

    if feasibility_payload:
        _append_artifact(
            feasibility_payload,
            "Feasibility Report",
            ["feasibility_pdf_url", "pdf_document"],
            "PDF",
            "Detailed viability analysis for shortlisted concepts",
            "Feasibility",
        )
    if prioritization_payload:
        _append_artifact(
            prioritization_payload,
            "Shortlist Scorecard",
            ["shortlist_pdf_url", "pdf_document"],
            "PDF",
            "Weighted shortlist and rationale for selection",
            "Prioritization",
        )
    if action_plan_payload:
        _append_artifact(
            action_plan_payload,
            "Action Plan",
            ["action_plan_pdf_url", "pdf_document"],
            "PDF",
            "Milestones, owners, and next steps",
            "Action Plan",
        )

    # Idea & cluster insights from canonical session (fallback to DB if needed)
    clusters_snapshot: list[dict[str, Any]] = []
    ideas_snapshot: list[dict[str, Any]] = []
    if canonical_session:
        raw_clusters = canonical_session.get('clusters')
        if isinstance(raw_clusters, list):
            for c in raw_clusters:
                if isinstance(c, dict):
                    clusters_snapshot.append(c)
        raw_ideas = canonical_session.get('ideas')
        if isinstance(raw_ideas, list):
            for idea in raw_ideas:
                if isinstance(idea, dict):
                    ideas_snapshot.append(idea)

    if not clusters_snapshot:
        try:
            latest_cluster_task = (
                BrainstormTask.query
                .filter_by(workshop_id=workshop.id, task_type='clustering_voting')
                .order_by(BrainstormTask.created_at.desc())
                .first()
            )
            cluster_rows = []
            if latest_cluster_task:
                cluster_rows = (
                    IdeaCluster.query
                    .filter_by(task_id=latest_cluster_task.id)
                    .order_by(IdeaCluster.id.asc())
                    .all()
                )
            for cluster in cluster_rows:
                clusters_snapshot.append({
                    'cluster_id': cluster.id,
                    'name': cluster.name,
                    'description': cluster.description,
                    'votes': cluster.votes.count() if hasattr(cluster, 'votes') and hasattr(cluster.votes, 'count') else 0,
                })
        except Exception:
            pass

    if not ideas_snapshot:
        try:
            idea_rows = (
                BrainstormIdea.query
                .join(BrainstormTask, BrainstormIdea.task_id == BrainstormTask.id)
                .filter(BrainstormTask.workshop_id == workshop.id)
                .order_by(BrainstormIdea.timestamp.asc())
                .limit(50)
                .all()
            )
            for idea in idea_rows:
                ideas_snapshot.append({
                    'idea_id': idea.id,
                    'text': idea.corrected_text or idea.content,
                    'participant_id': idea.participant_id,
                    'cluster_id': idea.cluster_id,
                })
        except Exception:
            pass

    def _sort_clusters(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            clusters,
            key=lambda c: (-(c.get('votes') or 0), (c.get('cluster_id') or 0)),
        )

    top_clusters = _sort_clusters(clusters_snapshot)[:6]
    top_ideas = ideas_snapshot[:8]

    # Journey snapshots by phase (framing, warm-up, etc.)
    phase_snapshots: list[dict[str, Any]] = []
    phase_order = [
        ("framing", "Framing", "How we defined the challenge"),
        ("warmup", "Warm-Up", "Energy builders and primers"),
        ("brainstorming", "Ideation", "Raw ideas captured"),
        ("clustering_voting", "Clustering & Voting", "Patterns and momentum"),
        ("feasibility", "Feasibility", "Risk + readiness insights"),
        ("prioritization", "Prioritization", "Shortlisted opportunities"),
        ("action_plan", "Action Plan", "Commitments and milestones"),
        ("discussion", "Discussion", "Key decisions & notes"),
    ]
    for key, title, subtitle in phase_order:
        raw_val = canonical_session.get(key) if canonical_session else None
        snapshot = None
        if isinstance(raw_val, (dict, list)):
            snapshot = raw_val
        elif isinstance(raw_val, str) and raw_val.strip():
            snapshot = raw_val
        if snapshot is not None:
            phase_snapshots.append({
                'key': key,
                'title': title,
                'subtitle': subtitle,
                'content': snapshot,
            })

    # Structured highlights for feasibility, prioritization, and action plan payloads
    feasibility_clusters: list[dict[str, Any]] = []
    feasibility_method_notes = None
    if isinstance(feasibility_payload, dict):
        analysis = feasibility_payload.get('analysis') if isinstance(feasibility_payload.get('analysis'), dict) else {}
        feasibility_method_notes = analysis.get('method_notes') if isinstance(analysis.get('method_notes'), str) else None
        clusters_data = analysis.get('clusters') if isinstance(analysis.get('clusters'), list) else []
        for cluster in clusters_data:
            if not isinstance(cluster, dict):
                continue
            scores = cluster.get('feasibility_scores') if isinstance(cluster.get('feasibility_scores'), dict) else {}
            findings = cluster.get('findings') if isinstance(cluster.get('findings'), dict) else {}
            recommendation = cluster.get('recommendation') if isinstance(cluster.get('recommendation'), dict) else {}
            feasibility_clusters.append({
                'cluster_name': cluster.get('cluster_name') or cluster.get('name') or cluster.get('title'),
                'votes': cluster.get('votes'),
                'scores': scores,
                'findings': findings,
                'recommendation': recommendation,
                'representative_ideas': cluster.get('representative_ideas') if isinstance(cluster.get('representative_ideas'), list) else [],
            })

    prioritized_items: list[dict[str, Any]] = []
    prioritization_methods: list[str] = []
    if isinstance(prioritization_payload, dict):
        raw_prioritized = prioritization_payload.get('prioritized') if isinstance(prioritization_payload.get('prioritized'), list) else []
        for item in raw_prioritized:
            if not isinstance(item, dict):
                continue
            scores = item.get('scores') if isinstance(item.get('scores'), dict) else {}
            prioritized_items.append({
                'rank': item.get('rank'),
                'title': item.get('title') or item.get('theme_label') or item.get('cluster_name'),
                'description': item.get('description'),
                'vote_count': item.get('vote_count'),
                'scores': scores,
                'position': item.get('position'),
                'why': item.get('why'),
                'representative_ideas': item.get('representative_ideas') if isinstance(item.get('representative_ideas'), list) else [],
            })
        methods_val = prioritization_payload.get('methods')
        if isinstance(methods_val, list):
            prioritization_methods = [str(m) for m in methods_val if isinstance(m, (str, int, float))]

    action_plan_actions: list[dict[str, Any]] = []
    action_plan_milestones: list[dict[str, Any]] = []
    if isinstance(action_plan_payload, dict):
        raw_actions = action_plan_payload.get('action_items') if isinstance(action_plan_payload.get('action_items'), list) else []
        for action in raw_actions:
            if isinstance(action, dict):
                action_plan_actions.append(action)
        raw_milestones = action_plan_payload.get('milestones') if isinstance(action_plan_payload.get('milestones'), list) else []
        for milestone in raw_milestones:
            if isinstance(milestone, dict):
                action_plan_milestones.append(milestone)

    # Linked documents gallery (all artifacts associated with workshop)
    documents_gallery: list[dict[str, Any]] = []
    try:
        doc_links = (
            WorkshopDocument.query
            .options(selectinload(WorkshopDocument.document))
            .filter(WorkshopDocument.workshop_id == workshop.id)
            .order_by(WorkshopDocument.added_at.asc())
            .all()
        )
        for link in doc_links:
            doc = getattr(link, 'document', None)
            if not doc:
                continue
            url_guess = None
            try:
                if doc.file_path:
                    url_guess = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{os.path.basename(doc.file_path)}"
            except Exception:
                url_guess = None
            documents_gallery.append({
                'id': doc.id,
                'title': doc.title,
                'description': doc.description,
                'file_name': doc.file_name,
                'file_size': doc.file_size,
                'url': url_guess,
                'linked_at': link.added_at,
            })
    except Exception as exc:
        current_app.logger.warning("[Report] Failed to build documents gallery for workshop %s: %s", workshop.id, exc, exc_info=True)

    # --- Render markdown / structured HTML for pre-workshop generated content ---
    def _render_agenda_html(raw):
        if not raw:
            return "<em>None</em>"
        # Try to parse JSON structure first (agenda generator often returns JSON)
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("agenda"), list):
                items = list(data.get("agenda") or [])
                lis = []
                for it in items:
                    # Each item may be dict with activity/title/description
                    if isinstance(it, dict):
                        text = it.get("activity") or it.get("title") or it.get("description") or "Activity"
                    else:
                        text = str(it)
                    # Render simple escaped text to avoid nested <p> inside <li>
                    lis.append(f"<li>{escape(text)}</li>")
                return f"<ol class=\"mb-0 ps-3\">{''.join(lis)}</ol>"
        except Exception:
            pass  # Fall back to markdown rendering below
        # Treat as markdown text
        try:
            return markdown.markdown(raw)
        except Exception:
            return f"<pre class='mb-0 small'>{escape(raw)}</pre>"

    # Prefer normalized agenda rows if present
    agenda_rows = []
    try:
        agenda_rows = WorkshopAgenda.query.filter_by(workshop_id=workshop.id).order_by(WorkshopAgenda.position.asc()).all()
    except Exception:
        agenda_rows = []
    if agenda_rows:
        lis = []
        for r in agenda_rows:
            title = r.activity_title or 'Activity'
            if r.estimated_duration:
                title = f"{title} <span class='text-body-secondary small'>({int(r.estimated_duration)} min)</span>"
            lis.append(f"<li>{title}</li>")
        agenda_html = f"<ol class='mb-0 ps-3'>{''.join(lis)}</ol>"
    else:
        agenda_html = _render_agenda_html(workshop.agenda)
    rules_html = markdown.markdown(workshop.rules) if workshop.rules else "<em>None</em>"
    icebreaker_html = markdown.markdown(workshop.icebreaker) if workshop.icebreaker else "<em>None</em>"
    tip_html = markdown.markdown(workshop.tip) if workshop.tip else "<em>None</em>"

    # Load action items for this workshop (ordered by status then due date)
    try:
        action_items = (
            ActionItem.query.filter_by(workshop_id=workshop.id)
            .order_by(ActionItem.status.asc(), ActionItem.due_date.asc().nullslast(), ActionItem.created_at.asc())
            .all()
        )
    except Exception:
        action_items = []

    return render_template(
        "workshop_report.html",
        workshop=workshop,
        participants=participants,
        current_participant=participant,
        can_edit=can_edit,
        participants_payload=participants_payload,
        agenda_html=agenda_html,
        rules_html=rules_html,
        icebreaker_html=icebreaker_html,
        tip_html=tip_html,
        summary_html=summary_html,
        action_items=action_items,
        transcript=transcript,
        timeline=timeline,
        ideas_count=total_ideas_count,
        clusters_count=total_clusters_count,
        summary_payload=summary_payload,
        feasibility_payload=feasibility_payload,
        prioritization_payload=prioritization_payload,
        action_plan_payload=action_plan_payload,
        canonical_session=canonical_session,
        report_artifacts=report_artifacts,
        top_clusters=top_clusters,
        top_ideas=top_ideas,
        phase_snapshots=phase_snapshots,
        documents_gallery=documents_gallery,
        feasibility_clusters=feasibility_clusters,
        feasibility_method_notes=feasibility_method_notes,
        prioritized_items=prioritized_items,
        prioritization_methods=prioritization_methods,
        action_plan_actions=action_plan_actions,
        action_plan_milestones=action_plan_milestones,
    )
    # TODO: Pass report data here



# --- Begin Workshop Introduction Task ---
@workshop_bp.route("/<int:workshop_id>/begin_intro", methods=["POST"])
@login_required
def begin_intro(workshop_id):
    """Begin the workshop by starting the first configured plan item.
    This replaces the legacy behavior that always started a warm-up.
    """
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify(success=False, message="Permission denied"), 403

    # Prevent double-start; allow when scheduled or inprogress with no active task
    if workshop.current_task_id:
        return jsonify(success=False, message="A task is already active."), 400
    if workshop.status not in ['scheduled', 'inprogress']:
        return jsonify(success=False, message="Workshop cannot be started in the current status."), 400

    # Move to inprogress if starting from scheduled
    if workshop.status == 'scheduled':
        workshop.status = 'inprogress'
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify(success=False, message="Failed to update workshop status."), 500

    # Start whatever is at index 0 of the configured plan
    try:
        ok, payload_or_error = go_to_task(workshop_id, 0)
        if not ok:
            return jsonify(success=False, message=str(payload_or_error)), 400
        return jsonify(success=True)
    except Exception as e:
        current_app.logger.error(f"Error starting first plan task for {workshop_id}: {e}", exc_info=True)
        return jsonify(success=False, message="Server error starting workshop."), 500


@workshop_bp.route("/<int:workshop_id>/switch_warmup", methods=["POST"])
@login_required
def switch_warmup_option(workshop_id: int):
    """Allow the organizer to switch to a different warm-up option mid-task."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify(success=False, message="Only the organizer can switch warm-up options."), 403

    payload = request.get_json(silent=True) or {}
    raw_index = payload.get("option_index")
    if raw_index is None:
        return jsonify(success=False, message="Invalid option index."), 400
    try:
        option_index = int(raw_index)
    except (TypeError, ValueError):
        return jsonify(success=False, message="Invalid option index."), 400

    task: BrainstormTask | None = workshop.current_task
    if not task and workshop.current_task_id:
        task = db.session.get(BrainstormTask, workshop.current_task_id)

    task_type = (task.task_type or "").strip().lower() if task and task.task_type else ""
    canonical_type = task_type.replace("_", "-")
    if canonical_type == "introduction":
        canonical_type = "warm-up"
    if not task or canonical_type != "warm-up":
        return jsonify(success=False, message="No active warm-up task to update."), 400

    try:
        task_payload = json.loads(task.payload_json) if task.payload_json else {}
    except Exception:
        task_payload = {}

    options = task_payload.get("options") if isinstance(task_payload, dict) else None
    if not isinstance(options, list) or not options:
        return jsonify(success=False, message="Warm-up options unavailable."), 400
    if option_index < 0 or option_index >= len(options):
        return jsonify(success=False, message="Option index out of range."), 400

    selected = options[option_index]
    if not isinstance(selected, dict):
        return jsonify(success=False, message="Selected option is invalid."), 400

    task_payload["selected_index"] = option_index
    task_payload["selected_option"] = selected

    new_title = selected.get("title") or task_payload.get("title") or "Warm-Up"
    new_description = selected.get("prompt") or task_payload.get("task_description") or task_payload.get("description")
    task_payload["title"] = new_title
    task_payload["task_description"] = new_description
    task_payload["description"] = new_description

    serialized = json.dumps(task_payload)
    task.title = new_title
    task.description = new_description
    task.prompt = serialized
    task.payload_json = serialized

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error("Failed to switch warm-up option for workshop %s: %s", workshop_id, exc, exc_info=True)
        return jsonify(success=False, message="Server error updating warm-up option."), 500

    broadcast_payload = dict(task_payload)
    broadcast_payload["task_id"] = task.id
    broadcast_payload["task_type"] = task.task_type or "warm-up"
    broadcast_payload["workshop_id"] = workshop_id
    try:
        remaining = workshop.get_remaining_task_time()
        if remaining:
            broadcast_payload["remaining_seconds"] = max(0, int(remaining))
    except Exception:
        pass
    try:
        warm_up_service.cache_warmup_payload(workshop_id, broadcast_payload)
    except Exception:
        current_app.logger.debug("[WarmUp] Cache update failed for workshop %s", workshop_id, exc_info=True)

    room = f"workshop_room_{workshop_id}"
    socketio.emit("warm_up_option_changed", broadcast_payload, to=room)
    current_app.logger.info(
        "[WarmUp] Organizer %s switched option %s for workshop %s",
        getattr(current_user, "user_id", None),
        option_index,
        workshop_id,
    )

    return jsonify(success=True, selected_index=option_index)


@workshop_bp.route("/<int:workshop_id>/warmup_state", methods=["GET"])
@login_required
def get_warmup_state(workshop_id: int):
    """Return the latest cached warm-up payload for reconnecting clients."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        participant = WorkshopParticipant.query.filter_by(
            workshop_id=workshop.id,
            user_id=getattr(current_user, "user_id", None),
        ).first()
        if not participant:
            return jsonify({"success": False, "message": "Permission denied."}), 403

    payload = warm_up_service.get_cached_warmup_payload(workshop_id)
    current_task_id = workshop.current_task_id
    status_allows_warmup = workshop.status in {"inprogress", "paused"}

    if not payload:
        task: BrainstormTask | None = workshop.current_task
        if not task and workshop.current_task_id:
            task = db.session.get(BrainstormTask, workshop.current_task_id)
        task_type = (task.task_type or "").strip().lower() if task and task.task_type else ""
        if task and task_type.replace("_", "-") in warm_up_service.WARM_TASK_ALIASES:
            try:
                raw = json.loads(task.payload_json or "{}") if task.payload_json else {}
            except Exception:
                raw = {}
            if isinstance(raw, dict) and raw:
                raw.setdefault("task_id", task.id)
                raw.setdefault("task_type", warm_up_service.WARM_TASK_TYPE)
                try:
                    warm_up_service.cache_warmup_payload(workshop_id, raw)
                except Exception:
                    current_app.logger.debug("[WarmUp] Cache hydrate failed for workshop %s", workshop_id, exc_info=True)
                payload = warm_up_service.get_cached_warmup_payload(workshop_id) or raw

    if not payload:
        return jsonify({"active": False})

    payload_task_id = payload.get("task_id")
    if not status_allows_warmup or not current_task_id:
        return jsonify({"active": False})

    try:
        if payload_task_id is not None and int(payload_task_id) != int(current_task_id):
            # Stale cache; clear and refuse to hydrate
            try:
                warm_up_service.clear_warmup_cache(workshop_id)
            except Exception:
                current_app.logger.debug(
                    "[WarmUp] Cleared stale cache for workshop %s", workshop_id, exc_info=True
                )
            return jsonify({"active": False})
    except Exception:
        return jsonify({"active": False})

    response_payload = copy.deepcopy(payload)
    response_payload.setdefault("task_id", workshop.current_task_id)
    response_payload.setdefault("task_type", warm_up_service.WARM_TASK_TYPE)
    response_payload["workshop_id"] = workshop_id
    try:
        remaining = workshop.get_remaining_task_time()
        if remaining is not None:
            response_payload["remaining_seconds"] = max(0, int(remaining))
    except Exception:
        pass

    return jsonify({"active": True, "payload": response_payload})


# =============================
# Idea submission (Whiteboard)
# =============================
@workshop_bp.route("/<int:workshop_id>/submit_idea", methods=["POST"])
@login_required
def submit_idea(workshop_id):
    """Accept a whiteboard idea submission for the current active task.

    Expects JSON body: { "task_id": number, "content": string }

    Returns JSON { success: true, id: idea_id } on success.
    Emits 'new_idea' to the workshop room so all clients update in real time.
    """
    workshop = Workshop.query.get_or_404(workshop_id)

    # Must be a participant in this workshop
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()
    if not participant:
        return jsonify(success=False, message="You are not a participant in this workshop."), 403

    # Workshop must be active
    if workshop.status != 'inprogress':
        return jsonify(success=False, message="Workshop is not in progress."), 400

    # Parse JSON body
    payload = request.get_json(silent=True) or {}
    task_id = payload.get("task_id")
    content = (payload.get("content") or "").strip()
    if not task_id or not content:
        return jsonify(success=False, message="task_id and content are required."), 400

    # Validate task
    if not workshop.current_task or workshop.current_task_id != task_id:
        return jsonify(success=False, message="No active task matching the submission."), 400

    task_type = (workshop.current_task.task_type or "").strip().lower()
    if task_type not in ("warm-up", "brainstorming", "discussion"):
        return jsonify(success=False, message="Ideas can only be submitted during idea phases."), 400

    # Create idea
    idea = BrainstormIdea()
    idea.task_id = int(task_id)
    idea.participant_id = participant.id
    idea.content = content
    idea.source = "human"
    idea.rationale = None
    idea.metadata_json = None
    idea.include_in_outputs = True

    try:
        db.session.add(idea)
        db.session.commit()

        # Broadcast to room
        try:
            username = (
                participant.user.first_name
                or (participant.user.email.split("@")[0] if participant.user and participant.user.email else None)
            ) or "Unknown"
        except Exception:
            username = "Unknown"

        emit_payload = {
            "idea_id": idea.id,
            "task_id": idea.task_id,
            "user": username,
            "content": idea.content,
            "timestamp": idea.timestamp.isoformat() if idea.timestamp else datetime.utcnow().isoformat(),
            "source": idea.source,
            "rationale": idea.rationale,
            "include_in_outputs": idea.include_in_outputs,
        }
        if idea.metadata_json:
            try:
                emit_payload["metadata"] = json.loads(idea.metadata_json)
            except Exception:
                emit_payload["metadata"] = idea.metadata_json
        room = f"workshop_room_{workshop_id}"
        socketio.emit("new_idea", emit_payload, to=room)

        # Optional: nudge inactive participants
        try:
            present_user_ids = list(_room_presence.get(room, set())) if isinstance(_room_presence.get(room, set()), set) else []
            check_and_nudge(workshop_id, current_user.user_id, present_user_ids)
        except Exception as e:
            current_app.logger.debug(f"[Moderator] nudge skipped: {e}")

        return jsonify(success=True, id=idea.id)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to submit idea for workshop {workshop_id}: {e}", exc_info=True)
        return jsonify(success=False, message="Server error while submitting idea."), 500


@workshop_bp.route("/workshop/<int:workshop_id>/ai_ideas_toggle", methods=["POST"])
@login_required
def toggle_ai_idea_inclusion(workshop_id: int) -> ResponseReturnValue:
    workshop = db.session.get(Workshop, workshop_id)
    if not workshop:
        return jsonify(success=False, message="Workshop not found."), 404

    if not is_organizer(workshop, current_user):
        return jsonify(success=False, message="Only organizers can toggle AI idea inclusion."), 403

    data = request.get_json(silent=True) or {}
    include = bool(data.get("include", False))

    # Determine the relevant brainstorming task (prefer current if active)
    task: BrainstormTask | None = None
    if workshop.current_task and (workshop.current_task.task_type or "").strip().lower() == "brainstorming":
        task = cast(BrainstormTask, workshop.current_task)
    if task is None:
        task = (
            BrainstormTask.query.filter_by(workshop_id=workshop_id, task_type="brainstorming")
            .order_by(BrainstormTask.created_at.desc())
            .first()
        )
    if not task:
        return jsonify(success=False, message="No brainstorming task available."), 400

    try:
        ai_ideas = (
            BrainstormIdea.query.filter_by(task_id=task.id, source="ai")
            .all()
        )
        for idea in ai_ideas:
            idea.include_in_outputs = include

        # Update persisted payload for future consumers
        try:
            payload_blob = json.loads(task.payload_json) if task.payload_json else {}
        except Exception:
            payload_blob = {}
        payload_blob["ai_ideas_include_in_outputs"] = include
        if "ai_ideas" in payload_blob and isinstance(payload_blob["ai_ideas"], list):
            for entry in payload_blob["ai_ideas"]:
                if isinstance(entry, dict) and entry.get("source") == "ai":
                    entry["include_in_outputs"] = include
        serialized = json.dumps(payload_blob)
        task.payload_json = serialized
        task.prompt = serialized

        db.session.commit()

        # Broadcast updated inclusion state and refreshed whiteboard
        room = f"workshop_room_{workshop_id}"
        try:
            ideas_payload: list[dict[str, Any]] = []
            ideas = (
                BrainstormIdea.query.filter_by(task_id=task.id)
                .order_by(BrainstormIdea.timestamp.asc())
                .all()
            )
            for idea in ideas:
                try:
                    username = (
                        idea.participant.user.first_name
                        or idea.participant.user.email.split("@")[0]
                        if idea.participant and idea.participant.user and idea.participant.user.email
                        else "Unknown"
                    )
                except Exception:
                    username = "Unknown"
                metadata_payload: Any = None
                raw_meta = getattr(idea, "metadata_json", None)
                if raw_meta:
                    try:
                        metadata_payload = json.loads(raw_meta)
                    except Exception:
                        metadata_payload = raw_meta
                ideas_payload.append(
                    {
                        "idea_id": idea.id,
                        "user": username,
                        "content": idea.content,
                        "timestamp": idea.timestamp.isoformat() if idea.timestamp else datetime.utcnow().isoformat(),
                        "source": getattr(idea, "source", "human"),
                        "rationale": getattr(idea, "rationale", None),
                        "metadata": metadata_payload,
                        "include_in_outputs": bool(getattr(idea, "include_in_outputs", True)),
                    }
                )
            socketio.emit(
                "whiteboard_sync",
                {"ideas": ideas_payload, "ai_ideas_include_in_outputs": include},
                to=room,
            )
        except Exception as broadcast_err:
            current_app.logger.debug(
                "[Brainstorming] Failed to broadcast whiteboard after AI inclusion toggle: %s",
                broadcast_err,
                exc_info=True,
            )

        socketio.emit(
            "ai_idea_inclusion",
            {"include": include, "task_id": task.id, "workshop_id": workshop_id},
            to=room,
        )

        return jsonify(success=True, include=include)
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "[Brainstorming] Failed to toggle AI idea inclusion for workshop %s: %s",
            workshop_id,
            exc,
            exc_info=True,
        )
        return jsonify(success=False, message="Unable to update AI idea inclusion."), 500


# ################################################################################
# WORKSHOP TASK MANAGEMENT
##################################################################################

# =============================
# Agenda (Normalized Rows) API
# =============================

def _ensure_organizer(workshop: Workshop):
    if not workshop:
        abort(404)
    if current_user.user_id != workshop.created_by_id:
        abort(403)

def _parse_time_slot_to_offsets(time_slot: str):
    """Parses 'HH:MM:SS - HH:MM:SS' into (start_seconds, end_seconds). Returns (None, None) if invalid."""
    try:
        if not time_slot or '-' not in time_slot:
            return None, None
        left, right = [s.strip() for s in time_slot.split('-', 1)]
        def to_sec(hms: str):
            parts = hms.split(':')
            if len(parts) != 3:
                return None
            h, m, s = map(int, parts)
            return h*3600 + m*60 + s
        start_s = to_sec(left)
        end_s = to_sec(right)
        if start_s is None or end_s is None:
            return None, None
        return start_s, end_s
    except Exception:
        return None, None

def _minutes_from_estimated(val: str):
    """Extracts minutes from strings like '30 min'. Returns int or None."""
    if not val:
        return None
    try:
        import re
        m = re.search(r"(\d+)", str(val))
        return int(m.group(1)) if m else None
    except Exception:
        return None

def _normalize_llm_item(it):
    """Accepts an LLM item with keys activity, description, estimated_duration, time_slot -> normalized dict."""
    title = it.get('activity') or it.get('activity_title') or ''
    desc = it.get('description') or it.get('activity_description') or None
    est = it.get('estimated_duration')
    est_minutes = _minutes_from_estimated(est) if isinstance(est, str) else (int(est) if isinstance(est, (int,float)) else None)
    ts = it.get('time_slot')
    start_offset, end_offset = _parse_time_slot_to_offsets(ts) if ts else (None, None)
    return dict(
        activity_title=title,
        activity_description=desc,
        estimated_duration=est_minutes,
        time_slot=ts,
        start_offset=start_offset,
        end_offset=end_offset,
    )

def _replace_agenda_rows(workshop_id: int, items: list, source: str = 'organizer'):
    # Remove existing
    WorkshopAgenda.query.filter_by(workshop_id=workshop_id).delete()
    # Insert new sequential positions
    pos = 1
    for raw in items:
        # Accept either normalized shape or LLM shape
        if ('activity' in raw) or ('estimated_duration' in raw and isinstance(raw.get('estimated_duration'), str)):
            data = _normalize_llm_item(raw)
        else:
            data = dict(
                activity_title=raw.get('activity_title', ''),
                activity_description=raw.get('activity_description'),
                estimated_duration=raw.get('estimated_duration'),
                time_slot=raw.get('time_slot'),
                start_offset=raw.get('start_offset'),
                end_offset=raw.get('end_offset'),
            )
        # Assign attributes explicitly to avoid constructor signature issues in static analysis
        rec = WorkshopAgenda()
        rec.workshop_id = workshop_id
        rec.position = pos
        rec.generated_source = source
        rec.activity_title = data.get('activity_title', '')
        rec.activity_description = data.get('activity_description')
        rec.estimated_duration = data.get('estimated_duration')
        rec.time_slot = data.get('time_slot')
        rec.start_offset = data.get('start_offset')
        rec.end_offset = data.get('end_offset')
        db.session.add(rec)
        pos += 1
    db.session.commit()


@workshop_bp.route('/<int:workshop_id>/agenda', methods=['GET'])
@login_required
def get_agenda(workshop_id):
    ws = Workshop.query.get_or_404(workshop_id)
    # Allow all attendees to view
    rows = WorkshopAgenda.query.filter_by(workshop_id=ws.id).order_by(WorkshopAgenda.position.asc()).all()
    def _s(r: WorkshopAgenda):
        return dict(
            id=r.id,
            workshop_id=r.workshop_id,
            position=r.position,
            activity_title=r.activity_title,
            activity_description=r.activity_description,
            estimated_duration=r.estimated_duration,
            generated_source=r.generated_source,
            created_at=r.created_at.isoformat() if r.created_at else None,
            updated_at=r.updated_at.isoformat() if r.updated_at else None,
            start_offset=r.start_offset,
            end_offset=r.end_offset,
            time_slot=r.time_slot,
        )
    return jsonify([_s(r) for r in rows])



# New: GET chat messages filtered by scope
@workshop_bp.route('/<int:workshop_id>/agenda', methods=['POST'])
@login_required
def save_agenda(workshop_id):
    ws = Workshop.query.get_or_404(workshop_id)
    _ensure_organizer(ws)
    payload = request.get_json(silent=True) or {}
    items = payload.get('items') or payload.get('agenda') or []
    source = payload.get('source') or 'organizer'
    if not isinstance(items, list):
        return jsonify({"error":"items must be an array"}), 400
    _replace_agenda_rows(ws.id, items, source)
    # optionally cache JSON
    ws.agenda = json.dumps({"agenda": items})
    db.session.commit()
    return jsonify({"status":"ok","count": len(items)})


@workshop_bp.route('/<int:workshop_id>/agenda/<int:item_id>', methods=['PATCH'])
@login_required
def update_agenda_item(workshop_id, item_id):
    ws = Workshop.query.get_or_404(workshop_id)
    _ensure_organizer(ws)
    row = WorkshopAgenda.query.filter_by(id=item_id, workshop_id=ws.id).first_or_404()
    p = request.get_json(silent=True) or {}
    if 'activity_title' in p: row.activity_title = p['activity_title']
    if 'activity_description' in p: row.activity_description = p['activity_description']
    if 'estimated_duration' in p: row.estimated_duration = int(p['estimated_duration']) if p['estimated_duration'] is not None else None
    if 'generated_source' in p: row.generated_source = p['generated_source']
    if 'time_slot' in p:
        row.time_slot = p['time_slot']
        so, eo = _parse_time_slot_to_offsets(row.time_slot)
        row.start_offset, row.end_offset = so, eo
    if 'start_offset' in p: row.start_offset = p['start_offset']
    if 'end_offset' in p: row.end_offset = p['end_offset']
    db.session.commit()
    return jsonify({"status":"ok"})


@workshop_bp.route('/<int:workshop_id>/agenda/reorder', methods=['PATCH'])
@login_required
def reorder_agenda(workshop_id):
    ws = Workshop.query.get_or_404(workshop_id)
    _ensure_organizer(ws)
    p = request.get_json(silent=True) or {}
    ids = p.get('ids') or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return jsonify({"error":"ids must be an array of integers"}), 400
    # Normalize: ensure all belong to this workshop
    rows = WorkshopAgenda.query.filter(WorkshopAgenda.id.in_(ids), WorkshopAgenda.workshop_id==ws.id).all()
    id_to_row = {r.id: r for r in rows}
    pos = 1
    for rid in ids:
        r = id_to_row.get(rid)
        if r:
            r.position = pos
            pos += 1
    db.session.commit()
    return jsonify({"status":"ok","count": pos-1})


@workshop_bp.route('/<int:workshop_id>/agenda/<int:item_id>', methods=['DELETE'])
@login_required
def delete_agenda_item(workshop_id, item_id):
    ws = Workshop.query.get_or_404(workshop_id)
    _ensure_organizer(ws)
    row = WorkshopAgenda.query.filter_by(id=item_id, workshop_id=ws.id).first_or_404()
    db.session.delete(row)
    # recompact positions
    others = WorkshopAgenda.query.filter_by(workshop_id=ws.id).order_by(WorkshopAgenda.position.asc()).all()
    for idx, r in enumerate(others, start=1):
        r.position = idx
    db.session.commit()
    return jsonify({"status":"ok"})


@workshop_bp.route('/<int:workshop_id>/regenerate/agenda', methods=['POST'])
@login_required
def regenerate_agenda_rows(workshop_id):  # legacy route name retained; delegate to unified implementation
    # Avoid duplicating logic: call the primary regenerate_agenda (fallback-capable) implementation.
    return regenerate_agenda(workshop_id)
