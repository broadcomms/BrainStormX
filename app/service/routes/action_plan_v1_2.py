
# app/service/routes/action_plan.py
"""Action Plan task payload generator and artifact builder.

Builds action items from shortlist-like inputs, with owner heuristic mapping,
optional LLM enrichment (if adapter is configured), a ReportLab PDF, and TTS script.
UX is identical to results_feasibility on the frontend (inline PDF viewer and broadcast sync).
"""
from __future__ import annotations

import os
import json
from typing import Any, Dict, List, Tuple, Union
from datetime import datetime

from flask import current_app

from app.extensions import db
from app.models import BrainstormTask, Workshop
from app.tasks.registry import TASK_REGISTRY

# Reuse helpers from presentation module (until we extract to a common util)
from app.service.routes.presentation import (
    _build_shortlist,
    _derive_action_plan_from_shortlist,
    _generate_action_plan_pdf,
    _extract_milestones,
)
from app.service.llm_adapter import (
    build_action_plan_with_meta_from_llm,
    build_prioritized_with_meta_from_llm,
)


def get_action_plan_payload(workshop_id: int, phase_context: str | None = None) -> Union[Dict[str, Any], tuple[str, int], None]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return None

    # LLM-only: derive prioritized list and action plan via LLM, no heuristics
    llm_used = False
    llm_error: str | None = None
    actions: List[Dict[str, Any]] = []
    milestones: List[Dict[str, Any]] = []
    tts_script: str = ""
    try:
        _raw_invoke = getattr(current_app, 'llm_invoke', None)
        def llm_invoke(prompt: str) -> str:
            try:
                if callable(_raw_invoke):
                    res = _raw_invoke(prompt)
                    return str(res) if res is not None else ""
            except Exception:
                return ""
            return ""
        if callable(_raw_invoke):
            # Build candidates context from latest clustering similar to prioritization route
            objective = getattr(ws, 'objective', '') or ''
            latest_cluster_task = (
                BrainstormTask.query
                .filter_by(workshop_id=ws.id, task_type="clustering_voting")
                .order_by(BrainstormTask.started_at.desc().nullslast(), BrainstormTask.id.desc())
                .first()
            )
            clusters: List[Dict[str, Any]] = []
            vote_counts: Dict[int, int] = {}
            candidates: List[Dict[str, Any]] = []
            if latest_cluster_task:
                from app.models import IdeaCluster, IdeaVote, BrainstormIdea, WorkshopParticipant
                cl = IdeaCluster.query.filter_by(task_id=latest_cluster_task.id).all()
                for c in cl:
                    clusters.append({"id": c.id, "name": c.name, "summary": c.description})
                    try:
                        vote_counts[c.id] = int(IdeaVote.query.filter_by(cluster_id=c.id).count())
                    except Exception:
                        vote_counts[c.id] = 0
                ideas = BrainstormIdea.query.filter(BrainstormIdea.cluster_id.in_([c.id for c in cl])).all()
                for i in ideas:
                    title = (i.content or "").strip()
                    if not title:
                        continue
                    candidates.append({"id": i.id, "title": title[:160], "cluster_id": i.cluster_id, "votes_norm": None})
                # Participants roster
                participants = []
                try:
                    parts = WorkshopParticipant.query.filter_by(workshop_id=ws.id).all()
                    for p in parts:
                        u = getattr(p, 'user', None)
                        participants.append({
                            "participant_id": p.id,
                            "first_name": getattr(u, 'first_name', None),
                            "last_name": getattr(u, 'last_name', None),
                            "email": getattr(u, 'email', None),
                        })
                except Exception:
                    participants = []
                # First: prioritized with meta
                pri = build_prioritized_with_meta_from_llm(
                    objective=objective,
                    clusters=clusters,
                    vote_counts=vote_counts,
                    candidates=candidates,
                    invoke=llm_invoke,
                )
                prioritized = pri.get("prioritized") or []
                # Next: action plan with meta (includes milestones and tts)
                ap = build_action_plan_with_meta_from_llm(
                    prioritized=prioritized,
                    participants=participants,
                    invoke=llm_invoke,
                )
                actions = ap.get("action_items") or []
                milestones = ap.get("milestones") or []
                tts_script = ap.get("tts_script") or ""
                if actions:
                    llm_used = True
                else:
                    llm_error = "Empty or invalid LLM response for action plan."
    except Exception as e:
        llm_error = f"LLM action plan error: {e}"
        current_app.logger.error(f"[ActionPlan] LLM error: {e}", exc_info=True)

    # Strict mode note:
    # Do NOT abort the phase here. Instead, surface the error via payload (llm_error)
    # so the UI can render the developer banner with schema examples.
    # This preserves navigation flow while making failures visible in dev/test.
    try:
        from app.config import Config  # noqa: F401
        # Intentionally no early return; llm_error is added to payload below when DEBUG/TESTING
    except Exception:
        pass

    # Do not enrich heuristically; rely only on LLM output (milestones may be empty list)

    # PDF (use LLM milestones if present; in dev/test when LLM fails, use heuristic shortlist->plan for PDF preview)
    try:
        actions_for_pdf = actions
        milestones_for_pdf = milestones
        if (not llm_used) and (current_app.config.get("DEBUG") or current_app.config.get("TESTING")):
            try:
                # Build a heuristic shortlist and derive a basic plan for PDF preview only
                weights = {"votes": 0.0, "feasibility": 0.0, "objective_fit": 0.0}
                shortlist, _r = _build_shortlist(ws.id, weights, {})
                if isinstance(shortlist, list) and shortlist:
                    actions_for_pdf = _derive_action_plan_from_shortlist(ws.id, shortlist)
                    milestones_for_pdf = _extract_milestones(actions_for_pdf)
            except Exception:
                pass
        rel, _ = _generate_action_plan_pdf(ws, actions_for_pdf, milestones_for_pdf)
        url = f"{current_app.config.get('MEDIA_REPORTS_URL_PREFIX','/media/reports')}/{os.path.basename(rel)}"
    except Exception as e:
        current_app.logger.warning(f"[ActionPlan] PDF error: {e}")
        rel = url = ""

    # TTS overview from LLM only
    script = tts_script if llm_used else ""

    payload: Dict[str, Any] = {
        "title": "Action Plan",
        "task_type": "results_action_plan",
        "task_description": "Review the proposed action plan. Owners are heuristic-mapped; adjust as needed.",
        "instructions": "Walk through items, confirm owners and due dates, and capture edits live.",
        "task_duration": int(TASK_REGISTRY.get("results_action_plan", {}).get("default_duration", 900)),
        "action_items": actions,
        "milestones": milestones,
        "action_plan_pdf_path": rel,
        "action_plan_pdf_url": url,
        "tts_script": script,
        "narration": script,
        "llm_used": llm_used,
        **({"llm_error": llm_error} if (llm_error and (current_app.config.get("DEBUG") or current_app.config.get("TESTING"))) else {}),
        "phase_context": phase_context or "",
    }

    task = BrainstormTask()
    task.workshop_id = workshop_id
    task.task_type = "results_action_plan"
    task.title = payload["title"]
    task.description = payload.get("task_description")
    task.duration = int(payload.get("task_duration", 900))
    task.status = "pending"
    s = json.dumps(payload)
    task.prompt = s
    task.payload_json = s
    db.session.add(task)
    db.session.flush()

    payload["task_id"] = task.id
    current_app.logger.info(f"[ActionPlan] Created task {task.id} for workshop {workshop_id}")
    return payload


def _build_action_plan_tts(actions: List[Dict[str, Any]]) -> str:
    if not actions:
        return "No action items were identified."
    tops = ", ".join((a.get("title", "").strip() or "(untitled)") for a in actions[:5])
    return (
        "Here is the proposed action plan prioritizing early wins and ownership clarity. "
        f"First items include: {tops}."
    )


def rebuild_action_plan_artifacts(workshop_id: int, weights: Dict[str, float] | None = None, constraints: Dict[str, Any] | None = None) -> Dict[str, Any]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return {"error": "Workshop not found"}
    # In strict LLM mode, rebuilding should also invoke LLM; keep weights/constraints unused here
    actions: List[Dict[str, Any]] = []
    url = rel = ""
    script = ""
    try:
        _raw_invoke = getattr(current_app, 'llm_invoke', None)
        def llm_invoke(prompt: str) -> str:
            try:
                if callable(_raw_invoke):
                    res = _raw_invoke(prompt)
                    return str(res) if res is not None else ""
            except Exception:
                return ""
            return ""
        if callable(_raw_invoke):
            # Reuse logic from get_action_plan_payload
            objective = getattr(ws, 'objective', '') or ''
            latest_cluster_task = (
                BrainstormTask.query
                .filter_by(workshop_id=ws.id, task_type="clustering_voting")
                .order_by(BrainstormTask.started_at.desc().nullslast(), BrainstormTask.id.desc())
                .first()
            )
            clusters: List[Dict[str, Any]] = []
            vote_counts: Dict[int, int] = {}
            candidates: List[Dict[str, Any]] = []
            if latest_cluster_task:
                from app.models import IdeaCluster, IdeaVote, BrainstormIdea, WorkshopParticipant
                cl = IdeaCluster.query.filter_by(task_id=latest_cluster_task.id).all()
                for c in cl:
                    clusters.append({"id": c.id, "name": c.name, "summary": c.description})
                    try:
                        vote_counts[c.id] = int(IdeaVote.query.filter_by(cluster_id=c.id).count())
                    except Exception:
                        vote_counts[c.id] = 0
                ideas = BrainstormIdea.query.filter(BrainstormIdea.cluster_id.in_([c.id for c in cl])).all()
                for i in ideas:
                    title = (i.content or "").strip()
                    if not title:
                        continue
                    candidates.append({"id": i.id, "title": title[:160], "cluster_id": i.cluster_id, "votes_norm": None})
                parts = WorkshopParticipant.query.filter_by(workshop_id=ws.id).all()
                participants = []
                for p in parts:
                    u = getattr(p, 'user', None)
                    participants.append({
                        "participant_id": p.id,
                        "first_name": getattr(u, 'first_name', None),
                        "last_name": getattr(u, 'last_name', None),
                        "email": getattr(u, 'email', None),
                    })
                pri = build_prioritized_with_meta_from_llm(
                    objective=objective,
                    clusters=clusters,
                    vote_counts=vote_counts,
                    candidates=candidates,
                    invoke=llm_invoke,
                )
                ap = build_action_plan_with_meta_from_llm(
                    prioritized=(pri.get("prioritized") or []),
                    participants=participants,
                    invoke=llm_invoke,
                )
                actions = ap.get("action_items") or []
                script = ap.get("tts_script") or ""
    except Exception:
        pass
    try:
        rel, _ = _generate_action_plan_pdf(ws, actions, [])
        url = f"{current_app.config.get('MEDIA_REPORTS_URL_PREFIX','/media/reports')}/{os.path.basename(rel)}"
    except Exception:
        rel = url = ""
    return {
        "mode": "results_action_plan",
        "action_items": actions,
        "action_plan_pdf_path": rel,
        "action_plan_pdf_url": url,
        "tts_script": script,
    }
