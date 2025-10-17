# app/service/routes/presentation.py
"""Presentation task payload generator and artifact builder.

Modes:
- slideshow: Present a linked document/deck.
- shortlisting (default): Build a prioritized shortlist from clusters/ideas using equal weights,
    allow runtime tweaks via optional weights/constraints.
- action_plan: Generate initial action items (owner heuristic mapping allowed).

Implements pure-Python PDF export via ReportLab for the shortlist/action plan with naming:
    "{workshop_title} shortlist {YYYY-MM-DD} {HH-MM-SS}.pdf"
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from app.extensions import db
from app.models import BrainstormTask, Workshop, WorkshopDocument, WorkshopPlanItem, Document, IdeaCluster, BrainstormIdea, IdeaVote, WorkshopParticipant
from app.tasks.registry import TASK_REGISTRY
from app.service.llm_adapter import (
    build_prioritized_from_llm,
    build_action_plan_from_llm,
    build_milestones_from_llm,
)
from app.config import Config
from app.utils.value_parsing import bounded_int, safe_float, safe_int


def _clamp_weight(value: Any, default: float = 1.0) -> float:
    """Normalize weight inputs into the 0–5 range with sensible defaults."""
    parsed = safe_float(value, default=default)
    if parsed is None:
        parsed = default
    if parsed < 0.0:
        return 0.0
    if parsed > 5.0:
        return 5.0
    return parsed


def _get_plan_item_config(workshop_id: int, ttype: str) -> dict | None:
    """Return config for NEXT matching plan item, prefer config_json over description."""
    ws = db.session.get(Workshop, workshop_id)
    current_idx = ws.current_task_index if ws and isinstance(ws.current_task_index, int) else -1
    try:
        q = (
            WorkshopPlanItem.query
            .filter_by(workshop_id=workshop_id, task_type=ttype, enabled=True)
            .order_by(WorkshopPlanItem.order_index.asc())
        )
        for item in q.all():
            try:
                if item.order_index is not None and int(item.order_index) <= current_idx:
                    continue
            except Exception:
                pass
            if getattr(item, 'config_json', None):
                try:
                    data = json.loads(item.config_json) if not isinstance(item.config_json, dict) else item.config_json
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
            if getattr(item, 'description', None):
                try:
                    data = json.loads(item.description)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return None
    except Exception:
        return None


def get_presentation_payload(workshop_id: int, phase_context: str | None = None):
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return "Workshop not found", 404

    cfg_raw = _get_plan_item_config(workshop_id, "presentation")
    cfg: Dict[str, Any] = dict(cfg_raw) if cfg_raw else {}
    # Presentation mode: 'slideshow' | 'shortlisting' | 'action_plan'
    mode_raw = cfg.get("mode") or cfg.get("type") or "shortlisting"
    mode = str(mode_raw).strip().lower()
    if mode not in ("slideshow", "shortlisting", "action_plan"):
        mode = "shortlisting"

    presenter_user_id = cfg.get("presenter_user_id")
    document_id = cfg.get("document_id")

    # Validate linked document if provided (be liberal in what we accept so the viewer can still render)
    doc_meta = None
    if document_id:
        # Prefer a formally linked document, but allow a direct document reference as a fallback
        try:
            link = WorkshopDocument.query.filter_by(workshop_id=workshop_id, document_id=document_id).first()
        except Exception:
            link = None
        d = None
        doc_pk = safe_int(document_id, default=0)
        d = None
        if doc_pk > 0:
            try:
                d = db.session.get(Document, doc_pk)
            except Exception:
                d = None
        if not (link or d):
            # Nothing resolvable
            document_id = None
        elif d:
            # Pull lightweight metadata for client rendering
            from flask import url_for
            doc_meta = {
                "id": d.id,
                "title": d.title,
                "file_name": d.file_name,
                "file_size": d.file_size,
                "url": url_for('document_bp.serve_document_file', document_id=d.id),
            }

    # Defaults vary by mode
    if mode == "slideshow":
        title = "Presentation: Slideshow"
        task_description = "Presenter will walk through the selected document or slides."
        instructions = "If you're the presenter, use the controls to advance slides. Others can follow along and post questions in chat."
    elif mode == "action_plan":
        title = "Presentation: Action Plan"
        task_description = "Review the proposed action plan. Owners are heuristic-mapped; adjust as needed."
        instructions = "Walk through items, confirm owners and due dates, and capture edits live."
    else:
        title = "Presentation: Prioritization & Shortlisting"
        task_description = "Review a prioritized shortlist of ideas emerging from votes, feasibility, and the objective."
        instructions = "Discuss the ordering, adjust weights/constraints if needed, and agree on a final shortlist."
    duration = safe_int(TASK_REGISTRY.get("presentation", {}).get("default_duration", 900), default=900)

    payload: Dict[str, Any] = {
        "title": title,
        "task_type": "presentation",
        "mode": mode,
        "task_description": task_description,
        "instructions": instructions,
        "task_duration": duration,
        "presenter_user_id": presenter_user_id,
        "document_id": document_id,
        "document": doc_meta,
        "initial_slide_index": safe_int(cfg.get("initial_slide_index"), default=1),
        "phase_context": phase_context or "",
    }

    # Equal weighting defaults (guardrailed); adjustments belong in plan settings
    raw_weights_obj = cfg.get("weights")
    weights_source: Dict[str, Any] = raw_weights_obj if isinstance(raw_weights_obj, dict) else {}
    weights = {
        "votes": _clamp_weight(weights_source.get("votes"), default=1.0),
        "feasibility": _clamp_weight(weights_source.get("feasibility"), default=1.0),
        "objective_fit": _clamp_weight(weights_source.get("objective_fit"), default=1.0),
    }
    raw_constraints_obj = cfg.get("constraints")
    constraints: Dict[str, Any] = raw_constraints_obj if isinstance(raw_constraints_obj, dict) else {}
    max_items_config: int | None = None
    if isinstance(constraints, dict) and "max_items" in constraints:
        parsed_max = safe_int(constraints.get("max_items"), default=0)
        if parsed_max > 0:
            max_items_config = bounded_int(parsed_max, default=parsed_max, minimum=1, maximum=50)
            constraints["max_items"] = max_items_config
        else:
            constraints.pop("max_items", None)

    if mode == "shortlisting":
        shortlist, rationale = _build_shortlist(ws.id, weights, constraints)
        payload["shortlist"] = shortlist
        payload["rationale"] = rationale
        # Optional LLM scoring pass (replace shortlist if configured)
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
                # Gather workshop objective and cluster/vote context
                objective = getattr(ws, 'objective', '') or ''
                latest_cluster_task = (
                    BrainstormTask.query
                    .filter_by(workshop_id=ws.id, task_type="clustering_voting")
                    .order_by(BrainstormTask.started_at.desc().nullslast(), BrainstormTask.id.desc())
                    .first()
                )
                clusters = []
                vote_counts = {}
                if latest_cluster_task:
                    cl = IdeaCluster.query.filter_by(task_id=latest_cluster_task.id).all()
                    for c in cl:
                        clusters.append({"id": c.id, "name": c.name, "summary": c.description})
                        try:
                            vote_counts[c.id] = int(IdeaVote.query.filter_by(cluster_id=c.id).count())
                        except Exception:
                            vote_counts[c.id] = 0
                candidates = [
                    {
                        "id": it.get("id"),
                        "title": it.get("label"),
                        "cluster_id": it.get("cluster_id"),
                        "votes_norm": it.get("votes_norm"),
                    }
                    for it in shortlist
                ]
                llm_prioritized = build_prioritized_from_llm(
                    objective=objective,
                    clusters=clusters,
                    vote_counts=vote_counts,
                    candidates=candidates,
                    invoke=llm_invoke,
                )
                if isinstance(llm_prioritized, list) and llm_prioritized:
                    # Keep score if provided; otherwise fall back to heuristic score
                    def _score_of(pid):
                        try:
                            return next((x.get("score") for x in llm_prioritized if x.get("id") == pid), None)
                        except Exception:
                            return None
                    for it in shortlist:
                        sc = _score_of(it.get("id"))
                        if sc is not None:
                            it["score"] = sc
                    # Attach optional impact/effort into shortlist items for charting
                    for it in shortlist:
                        try:
                            src = next((x for x in llm_prioritized if x.get("id") == it.get("id")), None)
                            if src and isinstance(src.get("scores"), dict):
                                it["scores"] = {"impact": src["scores"].get("impact"), "effort": src["scores"].get("effort")}
                        except Exception:
                            pass
        except Exception:
            pass
        # Provide normalized contract for downstream modules
        try:
            payload["prioritized"] = _normalize_prioritized(shortlist)
        except Exception:
            payload["prioritized"] = []
        # Generate PDF artifact in instance/uploads/reports
        try:
            pdf_path_rel, pdf_url = _generate_shortlist_pdf(ws, shortlist, weights, rationale)
            payload["shortlist_pdf_path"] = pdf_path_rel
            # Prefer serving via reports media route when possible
            if not pdf_url and pdf_path_rel:
                fname = os.path.basename(pdf_path_rel)
                pdf_url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{fname}"
            payload["shortlist_pdf_url"] = pdf_url
            # Contract: pdf_document is the primary link for viewers/export
            payload["pdf_document"] = pdf_url
        except Exception as e:
            current_app.logger.warning(f"[Presentation] PDF generation skipped for workshop {ws.id}: {e}")
        # Build a spoken summary for facilitator
        try:
            sl = payload.get('shortlist') or []
            lines = [
                "We will review a prioritized shortlist generated with equal weights across votes, feasibility, and objective fit.",
                f"The current shortlist includes {len(sl)} item{'s' if len(sl)!=1 else ''}."
            ]
            # Mention constraints when present (e.g., max_items)
            try:
                max_items_display = max_items_config
                if max_items_display is None and isinstance(constraints, dict):
                    candidate = constraints.get('max_items')
                    if isinstance(candidate, int) and candidate > 0:
                        max_items_display = candidate
                if isinstance(max_items_display, int) and max_items_display > 0:
                    plural = "s" if max_items_display != 1 else ""
                    lines.append(f"A maximum of {max_items_display} item{plural} was applied.")
            except Exception:
                pass
            for idx, it in enumerate(payload.get('shortlist') or [], start=1):
                name = (it.get('label') or '').strip()
                sc = it.get('score')
                lines.append(f"{idx}. {name} (score {sc})")
            payload["tts_script"] = "\n".join(lines)
            payload["narration"] = payload.get("tts_script")
        except Exception:
            pass
    elif mode == "action_plan":
        # Derive action items from shortlist-like pass (reuse function)
        shortlist, _ = _build_shortlist(ws.id, weights, constraints)
        actions = _derive_action_plan_from_shortlist(ws.id, shortlist)
        # Optional LLM action plan pass
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
                prioritized = payload.get("prioritized") or _normalize_prioritized(shortlist)
                llm_actions = build_action_plan_from_llm(
                    prioritized=prioritized,
                    participants=participants,
                    invoke=llm_invoke,
                )
                if isinstance(llm_actions, list) and llm_actions:
                    actions = llm_actions
        except Exception:
            pass
        # LLM enrichment (milestones, due dates, recommendations)
        try:
            enriched = _llm_enrich_action_plan(ws.id, actions, phase_context)
            if isinstance(enriched, list) and enriched:
                actions = enriched
        except Exception:
            pass
        payload["action_items"] = actions
        try:
            # Optional LLM milestones pass
            ms = None
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
                    ms = build_milestones_from_llm(action_items=actions, invoke=llm_invoke)
            except Exception:
                ms = None
            payload["milestones"] = (ms if isinstance(ms, list) and ms else _extract_milestones(actions))
        except Exception:
            payload["milestones"] = []
        try:
            pdf_path_rel, pdf_url = _generate_action_plan_pdf(ws, actions)
            payload["action_plan_pdf_path"] = pdf_path_rel
            if not pdf_url and pdf_path_rel:
                fname = os.path.basename(pdf_path_rel)
                pdf_url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{fname}"
            payload["action_plan_pdf_url"] = pdf_url
            payload["pdf_document"] = pdf_url
        except Exception as e:
            current_app.logger.warning(f"[Presentation] Action plan PDF generation skipped: {e}")
        # Build a spoken overview
        try:
            items = payload.get('action_items') or []
            lines = [
                "We will walk through the draft action plan derived from the prioritized shortlist.",
                f"The plan currently lists {len(items)} item{'s' if len(items)!=1 else ''}."
            ]
            for idx, it in enumerate(payload.get('action_items') or [], start=1):
                title = (it.get('title') or '').strip()
                owner = it.get('owner_participant_id')
                if owner:
                    lines.append(f"{idx}. {title} – owner participant {owner}")
                else:
                    lines.append(f"{idx}. {title}")
            payload["tts_script"] = "\n".join(lines)
            payload["narration"] = payload.get("tts_script")
        except Exception:
            pass
    # else slideshow: nothing extra

    task = BrainstormTask()
    task.workshop_id = workshop_id
    task.task_type = "presentation"
    task.title = payload["title"]
    task.description = payload.get("task_description")
    task.duration = safe_int(payload.get("task_duration", duration), default=duration)
    task.status = "pending"
    payload_str = json.dumps(payload)
    task.prompt = payload_str
    task.payload_json = payload_str
    db.session.add(task)
    db.session.flush()

    payload["task_id"] = task.id
    current_app.logger.info(f"[Presentation] Created task {task.id} for workshop {workshop_id}")
    return payload


# ---------- Helpers: Shortlist generation and PDFs ----------

def _build_shortlist(workshop_id: int, weights: Dict[str, float], constraints: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Compute a prioritized shortlist of ideas with equal weighting by default.

    Scoring heuristic:
      score = w_votes * normalized_votes + w_feas * feasibility_hint + w_obj * objective_fit_hint
    If feasibility/objective hints are unavailable, treat as 0.5 neutral.
    Returns (shortlist, rationale) where shortlist is list of {id, label, cluster_id?, score}.
    """
    # Latest clustering task as basis
    latest_cluster_task = (
        BrainstormTask.query
        .filter_by(workshop_id=workshop_id, task_type="clustering_voting")
        .order_by(BrainstormTask.started_at.desc().nullslast(), BrainstormTask.id.desc())
        .first()
    )
    if not latest_cluster_task:
        return ([], {"note": "No clustering task found."})
    clusters = IdeaCluster.query.filter_by(task_id=latest_cluster_task.id).all()
    if not clusters:
        return ([], {"note": "No clusters found."})

    # Vote counts per cluster (normalize later)
    cluster_votes = {}
    for c in clusters:
        try:
            v = int(IdeaVote.query.filter_by(cluster_id=c.id).count())
        except Exception:
            v = 0
        cluster_votes[c.id] = v
    max_votes = max(cluster_votes.values()) if cluster_votes else 1

    # Build candidate ideas from top clusters (or all)
    ideas = BrainstormIdea.query.filter(BrainstormIdea.cluster_id.in_([c.id for c in clusters])).all()
    candidates: List[Dict[str, Any]] = []
    for i in ideas:
        label = (i.content or "").strip()
        if not label:
            continue
        cv = cluster_votes.get(i.cluster_id or 0, 0)
        norm_votes = (cv / max_votes) if max_votes > 0 else 0.0
        # Heuristic placeholders (equal weight baseline): 0.5 neutral
        feas = 0.5
        objfit = 0.5
        score = float(weights.get("votes", 1.0)) * norm_votes + float(weights.get("feasibility", 1.0)) * feas + float(weights.get("objective_fit", 1.0)) * objfit
        candidates.append({
            "id": i.id,
            "label": label[:160],
            "cluster_id": i.cluster_id,
            "score": round(score, 4),
            "votes_norm": round(norm_votes, 4),
        })

    # Simple constraints: max_items
    max_items = None
    try:
        val = constraints.get("max_items") if isinstance(constraints, dict) else None
        if isinstance(val, (int, str)):
            max_items = max(1, int(val))
    except Exception:
        max_items = None

    # Sort by score desc and slice
    candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    shortlist = candidates[:max_items] if max_items else candidates[:10]

    rationale = {
        "weights": weights,
        "constraints": constraints,
        "method": "Equal-weight heuristic over votes, with neutral feasibility/objective placeholders.",
        "generated_at": datetime.utcnow().isoformat(),
    }
    return shortlist, rationale


def _reports_dir() -> str:
    base = os.path.join(current_app.instance_path, "uploads", "reports")
    os.makedirs(base, exist_ok=True)
    return base


def _name_for_pdf(workshop: Workshop, kind: str) -> Tuple[str, str]:
    # kind: "shortlist" | "action-plan"
    safe_title = (workshop.title or "Workshop").strip().replace("/", "-")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S")
    fname = f"{safe_title} {kind} {ts}.pdf"
    abs_path = os.path.join(_reports_dir(), fname)
    rel_path = os.path.join("uploads", "reports", fname)
    return abs_path, rel_path


def _generate_shortlist_pdf(workshop: Workshop, shortlist: List[Dict[str, Any]], weights: Dict[str, float], rationale: Dict[str, Any]) -> Tuple[str, str]:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    abs_path, rel_path = _name_for_pdf(workshop, "shortlist")
    doc = SimpleDocTemplate(abs_path, pagesize=LETTER)
    styles = getSampleStyleSheet()
    elements = []
    elements.append(Paragraph(f"{workshop.title} — Shortlist", styles['Title']))
    elements.append(Spacer(1, 12))
    try:
        method_text = ""
        if isinstance(rationale, dict):
            mt = rationale.get('method')
            if isinstance(mt, str) and mt.strip():
                method_text = mt.strip()
        elements.append(Paragraph(method_text or "LLM-driven prioritization.", styles['Normal']))
    except Exception:
        elements.append(Paragraph("LLM-driven prioritization.", styles['Normal']))
    elements.append(Spacer(1, 12))
    # Optional RICE/ICE columns if present on items (scores.impact/effort)
    have_ie = any(bool(it.get('scores')) and (it['scores'].get('impact') is not None or it['scores'].get('effort') is not None) for it in shortlist)
    cols = ["#", "Idea", "Score", "Votes (norm)"] + (["Impact", "Effort"] if have_ie else [])
    data = [cols]
    for idx, item in enumerate(shortlist, start=1):
        row = [str(idx), item.get("label", ""), f"{item.get('score',0):.2f}", f"{item.get('votes_norm',0):.2f}"]
        if have_ie:
            s = item.get('scores') or {}
            imp = s.get('impact')
            eff = s.get('effort')
            row += ["" if imp is None else str(imp), "" if eff is None else str(eff)]
        data.append(row)
    colWidths = [24, 300, 50, 70] + ([50, 50] if have_ie else [])
    table = Table(data, colWidths=colWidths)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.black),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 12))
    # Rationale summary
    elements.append(Paragraph("Rationale", styles['Heading3']))
    elements.append(Paragraph(f"Weights: {weights}", styles['Code']))
    if isinstance(rationale, dict):
        method = rationale.get('method') or 'Equal-weight heuristic.'
        elements.append(Paragraph(f"Method: {method}", styles['Code']))
        gen = rationale.get('generated_at') or ''
        if gen:
            elements.append(Paragraph(f"Generated: {gen}", styles['Code']))
    elements.append(Spacer(1, 12))
    # Impact–Effort legend/table
    if have_ie:
        ie_data = [["Legend", "Meaning"], ["Impact", "0–100 or 1–5 (normalized)"], ["Effort", "0–100 or 1–5 (normalized)"]]
        ie_table = Table(ie_data, colWidths=[100, 340])
        ie_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
            ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
        ]))
        elements.append(Paragraph("Impact–Effort", styles['Heading3']))
        elements.append(ie_table)
    doc.build(elements)

    # Build URL via static serve using document_bp? We store under instance/uploads; reuse send_from_directory path.
    # No dedicated route for reports yet; return relative path and empty URL placeholder.
    return rel_path, ""


def _heuristic_owner_map(workshop_id: int, name: str | None) -> int | None:
    if not name:
        return None
    name = str(name).strip().lower()
    parts = [p for p in name.replace("_", " ").replace("-", " ").split() if p]
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop_id).all()
    for p in participants:
        u = getattr(p, 'user', None)
        if not u:
            continue
        firstname = (getattr(u, 'first_name', '') or '').strip().lower()
        lastname = (getattr(u, 'last_name', '') or '').strip().lower()
        email = (getattr(u, 'email', '') or '').strip().lower()
        handle = email.split('@')[0] if '@' in email else email
        tokens = {firstname, lastname, handle}
        if any(tok and tok in parts for tok in tokens):
            return p.id
    return None


def _derive_action_plan_from_shortlist(workshop_id: int, shortlist: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for idx, item in enumerate(shortlist, start=1):
        title = item.get('label') or f"Action {idx}"
        # naive parse owner tokens like "@alice" or trailing "- Bob"
        owner_hint = None
        lab = title.lower()
        if '@' in lab:
            try:
                handle = lab.split('@', 1)[1].split()[0]
                owner_hint = handle
            except Exception:
                owner_hint = None
        elif '-' in lab:
            try:
                after = lab.split('-', 1)[1].strip()
                owner_hint = after.split()[0]
            except Exception:
                owner_hint = None
        owner_id = _heuristic_owner_map(workshop_id, owner_hint)
        actions.append({
            "title": title,
            "description": "",
            "owner_participant_id": owner_id,  # may be None
            "status": "todo",
        })
    return actions


def _generate_action_plan_pdf(workshop: Workshop, actions: List[Dict[str, Any]], milestones: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, str]:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    abs_path, rel_path = _name_for_pdf(workshop, "action plan")
    doc = SimpleDocTemplate(abs_path, pagesize=LETTER)
    styles = getSampleStyleSheet()
    elements = []
    elements.append(Paragraph(f"{workshop.title} — Action Plan", styles['Title']))
    elements.append(Spacer(1, 12))
    data = [["#", "Title", "Owner (participant id)", "Status", "Due", "Priority"]]
    for idx, a in enumerate(actions, start=1):
        data.append([
            str(idx),
            a.get("title", ""),
            str(a.get("owner_participant_id") or "—"),
            a.get("status", "todo"),
            a.get("due_date", "—"),
            str(a.get("priority") or "—"),
        ])
    table = Table(data, colWidths=[24, 280, 120, 60, 60, 50])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.black),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
    ]))
    elements.append(table)
    # Milestones table when provided (prefer LLM-provided list; do not derive heuristically here)
    try:
        ms = milestones or []
        if ms:
            elements.append(Spacer(1, 12))
            elements.append(Paragraph("Milestones", styles['Heading3']))
            mdata = [["Milestone", "Items (by index)"]]
            for m in ms:
                index_val = m.get('index')
                title = m.get('title') or f"Milestone {index_val}"
                arr = m.get('item_indices') or []
                mdata.append([f"{index_val}. {title}", ", ".join(str(x) for x in arr)])
            mtable = Table(mdata, colWidths=[240, 350])
            mtable.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
                ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
            ]))
            elements.append(mtable)
    except Exception:
        pass
    doc.build(elements)
    return rel_path, ""


# --------- Public helper for in-phase rebuild (no new task row) ---------
def rebuild_presentation_artifacts(
    workshop_id: int,
    mode: str | None = None,
    weights: Dict[str, float] | None = None,
    constraints: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Recompute shortlist or action plan artifacts with updated knobs.

    Does not create a BrainstormTask; returns only the updated artifacts and metadata:
      - for shortlisting: { mode, shortlist, rationale, shortlist_pdf_url, shortlist_pdf_path, tts_script }
      - for action_plan: { mode, action_items, action_plan_pdf_url, action_plan_pdf_path, tts_script }
    """
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return {"error": "Workshop not found"}

    # Normalize mode and defaults
    m = (mode or "shortlisting").strip().lower()
    if m not in ("slideshow", "shortlisting", "action_plan"):
        m = "shortlisting"
    w = weights or {"votes": 1.0, "feasibility": 1.0, "objective_fit": 1.0}
    c = constraints or {}

    result: Dict[str, Any] = {"mode": m}

    if m == "shortlisting":
        shortlist, rationale = _build_shortlist(ws.id, w, c)
        result["shortlist"] = shortlist
        result["rationale"] = rationale
        # PDF artifact
        try:
            rel, url = _generate_shortlist_pdf(ws, shortlist, w, rationale)
            result["shortlist_pdf_path"] = rel
            if not url and rel:
                fname = os.path.basename(rel)
                url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{fname}"
            result["shortlist_pdf_url"] = url
        except Exception as e:
            current_app.logger.warning(f"[Presentation] Rebuild shortlist PDF failed: {e}")
        # TTS summary (reuse logic similar to get_presentation_payload)
        try:
            lines = [
                "Updated prioritized shortlist generated with current weights across votes, feasibility, and objective fit.",
                f"The shortlist includes {len(shortlist)} item{'s' if len(shortlist)!=1 else ''}.",
            ]
            mi = c.get('max_items') if isinstance(c, dict) else None
            if mi:
                try:
                    mi_int = int(mi)
                    lines.append(f"A maximum of {mi_int} item{'s' if mi_int!=1 else ''} was applied.")
                except Exception:
                    pass
            for idx, it in enumerate(shortlist, start=1):
                name = (it.get('label') or '').strip()
                sc = it.get('score')
                lines.append(f"{idx}. {name} (score {sc})")
            result["tts_script"] = "\n".join(lines)
        except Exception:
            pass
        return result

    if m == "action_plan":
        shortlist, _ = _build_shortlist(ws.id, w, c)
        actions = _derive_action_plan_from_shortlist(ws.id, shortlist)
        result["action_items"] = actions
        try:
            rel, url = _generate_action_plan_pdf(ws, actions)
            result["action_plan_pdf_path"] = rel
            if not url and rel:
                fname = os.path.basename(rel)
                url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{fname}"
            result["action_plan_pdf_url"] = url
        except Exception as e:
            current_app.logger.warning(f"[Presentation] Rebuild action plan PDF failed: {e}")
        # TTS overview
        try:
            lines = [
                "Updated draft action plan derived from the latest shortlist.",
                f"The plan lists {len(actions)} item{'s' if len(actions)!=1 else ''}.",
            ]
            for idx, it in enumerate(actions, start=1):
                title = (it.get('title') or '').strip()
                owner = it.get('owner_participant_id')
                if owner:
                    lines.append(f"{idx}. {title} – owner participant {owner}")
                else:
                    lines.append(f"{idx}. {title}")
            result["tts_script"] = "\n".join(lines)
        except Exception:
            pass
        return result

    # slideshow: nothing to rebuild
    return {"mode": m, "note": "Slideshow mode has no rebuildable artifacts."}


# ---------- Utilities and LLM placeholders (non-blocking) ----------

def _normalize_prioritized(shortlist: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map shortlist items into a standardized 'prioritized' contract.

    Output fields:
      - id: idea id
      - title: primary label
      - score: composite score
      - cluster_id: cluster reference when available
      - votes_norm: normalized cluster votes
    """
    norm: List[Dict[str, Any]] = []
    for item in shortlist or []:
        norm.append({
            "id": item.get("id"),
            "title": item.get("label"),
            "score": item.get("score"),
            "cluster_id": item.get("cluster_id"),
            "votes_norm": item.get("votes_norm"),
        })
    return norm


def _llm_enrich_action_plan(
    workshop_id: int,
    actions: List[Dict[str, Any]],
    phase_context: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Placeholder enrichment hook.

    Attempts to enrich action items with light heuristics when LLM is not wired:
      - Adds a naive due_date cadence (every 14 days apart)
      - Infers a simple priority rank = order index
      - Retains owner_participant_id
    If future LLM integration is present, this function can dispatch and map outputs back into this schema.
    """
    enriched: List[Dict[str, Any]] = []
    base = datetime.utcnow()
    for idx, a in enumerate(actions or [], start=1):
        item = dict(a)
        try:
            # 2-week cadence per item
            due = base + timedelta(days=14 * idx)
            item.setdefault("due_date", due.date().isoformat())
        except Exception:
            item.setdefault("due_date", None)
        item.setdefault("priority", idx)
        enriched.append(item)
    return enriched


def _extract_milestones(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Derive a minimal milestone set from actions.

    Heuristic: bucket actions into three phases based on their order.
    Output fields per milestone: { title, index, item_indices }
    """
    if not actions:
        return []
    n = len(actions)
    # 3 buckets: early, mid, late
    b1 = list(range(1, max(1, n // 3) + 1))
    b2 = list(range(max(b1) + 1, max(max(b1) + 1, 2 * n // 3) + 1)) if n >= 3 else []
    b3 = list(range(max(b2) + 1 if b2 else max(b1) + 1, n + 1))
    milestones: List[Dict[str, Any]] = []
    if b1:
        milestones.append({"title": "Milestone 1 — Kickoff & Setup", "index": 1, "item_indices": b1})
    if b2:
        milestones.append({"title": "Milestone 2 — Build & Iterate", "index": 2, "item_indices": b2})
    if b3:
        milestones.append({"title": "Milestone 3 — Launch & Measure", "index": 3, "item_indices": b3})
    return milestones
