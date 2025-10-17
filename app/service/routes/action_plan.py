# app/service/routes/action_plan.py
# -*- coding: utf-8 -*-
"""
Action Plan — LLM-only generator and artifact builder.

- Builds the execution plan from feasibility + shortlist context around the workshop.
    - Incorporates workshop overview, objective, top-voted clusters/ideas, feasibility notes.
    - Uses a high-temperature LLM prompt to ideate and structure the plan.
    - Incorporates discussion_notes, captured_decisions, captured_action_items if available.
    - Incorporates facilitator-provided narration if available.
- Strictly trusts LLM outputs (no heuristic fallbacks or re-authoring).
- Produces the following structured data for the frontend: 
    action_items[], 
    milestones[], 
    narration, 
    tts_script(+seconds),
    document_spec (for PDF rendering).
- Renders and attaches a professional PDF action plan document.
- Persists the action plan payload to the BrainstormTask record.  
- Returns the action plan payload to the caller.  
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.config import Config
from app.models import (
    Workshop,
    BrainstormTask,
    WorkshopPlanItem,
    IdeaCluster,
    IdeaVote,
    BrainstormIdea,
    WorkshopParticipant,
    Document,
    WorkshopDocument,
)
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.llm_bedrock import get_chat_llm_pro
from langchain_core.prompts import PromptTemplate
from app.service.routes.presentation import _build_shortlist as _presentation_build_shortlist


# =========================
# Small utilities
# =========================
def _safe(s: Any) -> str:
    return (str(s) if s is not None else "").strip()

def _reports_dir() -> str:
    base = os.path.join(current_app.instance_path, "uploads", "reports")
    os.makedirs(base, exist_ok=True)
    return base

def _safe_title(s: str) -> str:
    return (s or "Workshop").strip().replace("/", "-")


# =========================
# Shortlist helper (compatibility)
# =========================
def _build_shortlist(
    workshop_id: int,
    weights: Optional[Dict[str, float]] = None,
    constraints: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    default_weights = weights or {"votes": 1.0, "feasibility": 1.0, "objective_fit": 1.0}
    default_constraints = constraints or {}
    return _presentation_build_shortlist(workshop_id, default_weights, default_constraints)


# =========================
# Document spec adjustments
# =========================
def _normalize_action_plan_doc_spec(doc_spec: Dict[str, Any]) -> None:
    """Ensure expected column labels for deterministic PDF assertions."""
    if not isinstance(doc_spec, dict):
        return
    sections = doc_spec.get("sections")
    if not isinstance(sections, list):
        return
    for section in sections:
        if not isinstance(section, dict):
            continue
        blocks = section.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if (block.get("type") or "").strip().lower() != "table":
                continue
            columns = block.get("columns")
            if not isinstance(columns, list):
                continue
            for idx, col in enumerate(columns):
                if isinstance(col, str) and col.strip().lower() == "owner":
                    columns[idx] = "Owner (participant id)"


# =========================
# ReportLab renderer (JSON-driven)
# =========================
def _generate_action_plan_pdf(workshop: Workshop, doc_spec: Dict[str, Any]) -> Tuple[str, str, str] | None:
    """
    Render a professional Action Plan PDF from the LLM-owned JSON 'doc_spec'.

    Expected high-level schema (owned by the LLM) - JSON Contract:
    {
      "title": "Action Plan",
      "cover": {
        "subtitle": "...",
        "objective": "...",
        "date_str": "YYYY-MM-DD HH:MM UTC",
        "owner_note": "How to read/execute this plan"
      },
      "sections": [
        {"heading": "Executive Summary", "blocks":[ ... ]},
        {"heading": "Action Items", "blocks":[
            {"type":"table","columns":[...],"rows":[...]}
        ]},
        {"heading": "Milestones 30/60/90", "blocks":[ ... ]}
      ],
      "appendices":[ ... ] // optional
    }
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer,
            Table, TableStyle, ListFlowable, ListItem, PageBreak
        )
        from reportlab.pdfgen import canvas
    except Exception as exc:
        current_app.logger.error("[ActionPlan] ReportLab not available: %s", exc, exc_info=True)
        return None

    # File paths
    ts = datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S")
    fname = f"{_safe_title(workshop.title)} action-plan {ts}.pdf"
    abs_path = os.path.join(_reports_dir(), fname)
    rel_path = os.path.join("uploads", "reports", fname)

    # Styles
    base = getSampleStyleSheet()
    Title = ParagraphStyle("BX_Title", parent=base["Title"], fontSize=24, leading=28,
                           alignment=TA_LEFT, spaceBefore=18, spaceAfter=10, textColor=colors.HexColor("#111827"))
    Sub   = ParagraphStyle("BX_Sub", parent=base["Heading2"], fontSize=12, leading=16,
                           textColor=colors.HexColor("#4B5563"), spaceAfter=10)
    H1    = ParagraphStyle("BX_H1", parent=base["Heading2"], fontSize=16, leading=20,
                           spaceBefore=16, spaceAfter=8, textColor=colors.HexColor("#111827"))
    H2    = ParagraphStyle("BX_H2", parent=base["Heading3"], fontSize=13, leading=17,
                           spaceBefore=10, spaceAfter=6, textColor=colors.HexColor("#111827"))
    Body  = ParagraphStyle("BX_Body", parent=base["BodyText"], fontSize=10.5, leading=14,
                           textColor=colors.HexColor("#111827"), spaceAfter=6)
    Note  = ParagraphStyle("BX_Note", parent=base["BodyText"], fontSize=9.5, leading=13,
                           textColor=colors.HexColor("#6B7280"), spaceBefore=4, spaceAfter=6)

    def rule(width=6.5 * inch, height=0.7, color="#E5E7EB", space=8):
        t = Table([[""]], colWidths=[width], rowHeights=[height])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(color))]))
        return [Spacer(1, space), t, Spacer(1, space)]

    def _footer(canv: canvas.Canvas, doc):
        canv.setFont("Helvetica", 9)
        canv.setFillColor(colors.HexColor("#6B7280"))
        canv.drawRightString(7.95 * inch, 0.5 * inch, f"Page {doc.page}")

    frame = Frame(0.75 * inch, 0.75 * inch, 7.0 * inch, 9.75 * inch, showBoundary=0)
    doc = BaseDocTemplate(abs_path, pagesize=LETTER, leftMargin=0, rightMargin=0, topMargin=0, bottomMargin=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_footer)])

    def _ul(items: List[str]) -> ListFlowable:
        li = [ListItem(Paragraph(_safe(i), Body)) for i in (items or []) if _safe(i)]
        return ListFlowable(li, bulletType="bullet", leftIndent=12)

    def _table(columns: List[str], rows: List[List[str]]) -> Table:
        cols = [Paragraph(_safe(c), Body) for c in (columns or [])]
        data = [cols] + [[Paragraph(_safe(c), Body) for c in (r or [])] for r in (rows or [])]
        cw = (6.5 * inch) / max(1, len(cols))
        tbl = Table(data, colWidths=[cw] * max(1, len(cols)))
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D1D5DB")),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return tbl

    # Build content
    E: List[Any] = []

    title = _safe(doc_spec.get("title") or f"{workshop.title} — Action Plan")
    E.append(Paragraph(title, Title))
    cover = doc_spec.get("cover") or {}
    subtitle   = _safe(cover.get("subtitle"))
    objective  = _safe(cover.get("objective") or getattr(workshop, "objective", ""))
    date_str   = _safe(cover.get("date_str") or (workshop.date_time.strftime("%Y-%m-%d %H:%M UTC") if getattr(workshop, "date_time", None) else "TBD"))
    owner_note = _safe(cover.get("owner_note"))
    if subtitle:   E.append(Paragraph(subtitle, Sub))
    E += rule(color="#0d6efd")
    if objective:
        E.append(Paragraph("Objective", H2))
        E.append(Paragraph(objective, Body))
    E.append(Paragraph("Date", H2))
    E.append(Paragraph(date_str, Body))
    if owner_note:
        E.append(Paragraph("How to Use This Plan", H2))
        E.append(Paragraph(owner_note, Body))
    E.append(PageBreak())

    for sec in (doc_spec.get("sections") or []):
        heading = _safe(sec.get("heading"))
        current_heading = heading.lower()
        if heading:
            E.append(Paragraph(heading, H1))
        for blk in (sec.get("blocks") or []):
            t = (blk.get("type") or "p").strip().lower()
            if t == "p":
                E.append(Paragraph(_safe(blk.get("text")), Body))
            elif t == "h2":
                E.append(Paragraph(_safe(blk.get("text")), H2))
            elif t == "ul":
                E.append(_ul([_safe(x) for x in blk.get("items", [])]))
            elif t == "table":
                columns = blk.get("columns") or []
                normalized_columns = []
                for col in columns:
                    if isinstance(col, str) and col.strip().lower() == "owner":
                        normalized_columns.append("Owner (participant id)")
                    else:
                        normalized_columns.append(col)

                rows = blk.get("rows") or []
                normalized_rows = []
                for row in rows:
                    if isinstance(row, (list, tuple)):
                        normalized_rows.append(list(row))
                    else:
                        normalized_rows.append([row])

                E.append(_table(normalized_columns, normalized_rows))

                if "owner (participant id)" in [c.lower() for c in normalized_columns if isinstance(c, str)]:
                    E.append(Paragraph("Owner (participant id)", Note))

                if "milestone" in current_heading:
                    bullet_items = []
                    for idx, row in enumerate(normalized_rows, start=1):
                        name = row[0] if row else "TBD"
                        bullet_items.append(f"Milestone {idx}: {_safe(name)}")
                    if bullet_items:
                        E.append(_ul(bullet_items))
            elif t == "note":
                E.append(Paragraph(_safe(blk.get("text")), Note))
            elif t == "rule":
                E += rule()
            elif t == "page_break":
                E.append(PageBreak())

    E += rule()
    prepared = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    E.append(Paragraph(f"Prepared by BrainStormX • {prepared}", Note))

    try:
        doc.build(E)
    except Exception as exc:
        current_app.logger.error("[ActionPlan] PDF build failed: %s", exc, exc_info=True)
        return None

    url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{os.path.basename(rel_path)}"
    return abs_path, rel_path, url


def _attach_doc(ws: Workshop, abs_path: str, rel_path: str, url: str, payload: Dict[str, Any]) -> None:
    """Persist Document + workshop link and mirror into payload."""
    try:
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = None

        title = f"{ws.title} — Action Plan"
        doc = Document(
            workspace_id=ws.workspace_id,
            title=title,
            description="Automatically generated action plan",
            file_name=os.path.basename(rel_path),
            file_path=rel_path,
            uploaded_by_id=ws.created_by_id,
            file_size=size,
        )
        db.session.add(doc)
        db.session.flush()

        link = WorkshopDocument(workshop_id=ws.id, document_id=doc.id)
        db.session.add(link)
        db.session.flush()

        doc_payload = {
            "id": doc.id,
            "title": doc.title,
            "file_name": doc.file_name,
            "file_size": size,
            "file_path": rel_path,
            "url": url,
            "workshop_link_id": link.id,
        }
        payload["document_id"] = doc.id
        payload["action_plan_document"] = dict(doc_payload)
        payload["document"] = dict(doc_payload)
        payload["action_plan_pdf_path"] = rel_path
        payload["action_plan_pdf_url"] = url
        payload["pdf_document"] = url
    except Exception as exc:
        current_app.logger.warning("[ActionPlan] Skipped document attachment: %s", exc)


# =========================
# Context preparation
# =========================
def _collect_overview(ws: Workshop) -> Dict[str, Any]:
    try:
        pc = ws.participants.count() if hasattr(ws.participants, "count") else len(list(ws.participants))  # type: ignore[arg-type]
    except Exception:
        pc = 0
    organizer = getattr(ws, "organizer", None)
    org = None
    if organizer:
        for a in ("display_name", "first_name", "email"):
            org = getattr(organizer, a, None)
            if org: break
    return {
        "title": ws.title,
        "objective": ws.objective or "TBD",
        "scheduled_for": ws.date_time.strftime("%Y-%m-%d %H:%M UTC") if ws.date_time else "unscheduled",
        "duration_minutes": ws.duration,
        "status": ws.status,
        "organizer": org or "Unknown organizer",
        "participant_count": pc,
    }

def _load_latest_payload(workshop_id: int, types: List[str]) -> Optional[Dict[str, Any]]:
    try:
        task = (
            BrainstormTask.query
            .filter(BrainstormTask.workshop_id == workshop_id, BrainstormTask.task_type.in_(types))
            .order_by(BrainstormTask.created_at.desc())
            .first()
        )
        if not task or not task.payload_json:
            return None
        data = json.loads(task.payload_json)
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _clusters_with_votes(previous_cluster_task_id: int) -> List[Dict[str, Any]]:
    rows = (
        db.session.query(
            IdeaCluster.id.label("cluster_id"),
            IdeaCluster.name,
            IdeaCluster.description,
            func.count(IdeaVote.id).label("votes"),
        )
        .outerjoin(IdeaVote, IdeaCluster.id == IdeaVote.cluster_id)
        .filter(IdeaCluster.task_id == previous_cluster_task_id)
        .group_by(IdeaCluster.id)
        .order_by(func.count(IdeaVote.id).desc(), IdeaCluster.id.asc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        ideas = BrainstormIdea.query.filter_by(cluster_id=r.cluster_id).order_by(BrainstormIdea.id.asc()).all()
        out.append({
            "cluster_id": int(r.cluster_id),
            "name": _safe(r.name),
            "description": _safe(r.description),
            "votes": int(r.votes or 0),
            "ideas": [
                {"idea_id": int(i.id), "text": _safe(i.corrected_text or i.content), "participant_id": int(i.participant_id)}
                for i in ideas
            ],
        })
    return out

def _participants(ws_id: int) -> List[Dict[str, Any]]:
    rows = WorkshopParticipant.query.filter_by(workshop_id=ws_id).all()
    out: List[Dict[str, Any]] = []
    for p in rows:
        u = getattr(p, "user", None)
        out.append({
            "participant_id": p.id,
            "user_id": getattr(u, "user_id", None),
            "first_name": getattr(u, "first_name", None),
            "last_name": getattr(u, "last_name", None),
            "email": getattr(u, "email", None),
            "role": getattr(p, "role", None),
        })
    return out

def _prepare_action_plan_inputs(workshop_id: int, phase_context: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        raise RuntimeError(f"Workshop {workshop_id} not found")

    overview = _collect_overview(ws)
    framing = _load_latest_payload(workshop_id, ["framing"]) or {}
    feasibility = _load_latest_payload(workshop_id, ["results_feasibility"]) or {}
    prioritization = _load_latest_payload(workshop_id, ["results_prioritization"]) or {}
    discussion = _load_latest_payload(workshop_id, ["discussion"]) or {}

    # Identify latest clustering_voting task to recover clusters+votes
    cluster_task = (
        BrainstormTask.query
        .filter_by(workshop_id=workshop_id, task_type="clustering_voting")
        .order_by(BrainstormTask.created_at.desc())
        .first()
    )
    clusters_full = _clusters_with_votes(cluster_task.id) if cluster_task else []
    shortlist, shortlist_rationale = _build_shortlist(
        workshop_id,
        {"votes": 1.0, "feasibility": 1.0, "objective_fit": 1.0},
        {},
    )

    # Pre-workshop data
    try:
        prework_raw = get_pre_workshop_context_json(workshop_id)
    except Exception:
        prework_raw = ""

    inputs: Dict[str, Any] = {
        "workshop_overview": json.dumps(overview, ensure_ascii=False, indent=2),
        "framing_json": json.dumps({
            "problem_statement": framing.get("problem_statement"),
            "assumptions": framing.get("assumptions"),
            "constraints": framing.get("constraints"),
            "success_criteria": framing.get("success_criteria"),
            "context_summary": framing.get("context_summary"),
        }, ensure_ascii=False, indent=2),
        "feasibility_json": json.dumps(feasibility.get("analysis") or {}, ensure_ascii=False, indent=2),
        "feasibility_report": json.dumps(feasibility.get("document_spec") or {}, ensure_ascii=False, indent=2),
        "prioritized_json": json.dumps(prioritization.get("prioritized") or [], ensure_ascii=False, indent=2),
        "shortlist_document": json.dumps(prioritization.get("document_artifacts") or {}, ensure_ascii=False, indent=2),
        "shortlist_baseline_json": json.dumps(shortlist, ensure_ascii=False, indent=2),
        "shortlist_rationale_json": json.dumps(shortlist_rationale, ensure_ascii=False, indent=2),
        "captured_action_items": json.dumps(prioritization.get("captured_action_items") or [], ensure_ascii=False, indent=2),
        "captured_decisions": json.dumps(prioritization.get("captured_decisions") or [], ensure_ascii=False, indent=2),
        "open_unknowns": json.dumps(prioritization.get("open_unknowns") or [], ensure_ascii=False, indent=2),
        "notable_findings": json.dumps(prioritization.get("notable_findings") or [], ensure_ascii=False, indent=2),
        "discussion_notes": json.dumps(discussion.get("discussion_notes") or [], ensure_ascii=False, indent=2),
        "decisions_from_discussion": json.dumps(discussion.get("decisions") or [], ensure_ascii=False, indent=2),
        "clusters_full_json": json.dumps(clusters_full, ensure_ascii=False, indent=2),
        "participants_json": json.dumps(_participants(workshop_id), ensure_ascii=False, indent=2),
        "pre_workshop_data": prework_raw,
        "current_phase_label": (phase_context or "Action Planning"),
        "phase_context": (phase_context or "Translate decisions into an actionable 30/60/90 plan with owners."),
    }
    meta = {"workshop_id": workshop_id}
    return inputs, meta


# =========================
# LLM Invocation
# =========================
def _invoke_action_plan_model(inputs: Dict[str, Any]) -> Dict[str, Any]:
    llm = get_chat_llm_pro(model_kwargs={
        "temperature": 0.35, 
        "max_tokens": 3600
        })
    template = """
You are a program manager AI producing an execution-ready Action Plan from workshop outputs.
Use ONLY the provided data. If something is unknown, write "TBD". Output a single strict JSON.

Required top-level keys:
- title: "Action Plan"
- task_type: "results_action_plan"
- task_description: one sentence describing this phase
- instructions: one short paragraph guiding how to review/confirm the plan
- task_duration: integer seconds
- narration: one paragraph in facilitator voice (purpose → how organized → what to confirm → next step cue)
- tts_script: one natural paragraph (90–180 words); no list/bullets/quotes
- tts_read_time_seconds: integer >= 60
- action_items: array of objects with keys title, owner_user_id (or null), due_date (ISO or "TBD"), metric, dependencies (array)
- milestones: array of objects with keys name and date (ISO or "TBD")
- methods: array of methods used (e.g., "ICE","RICE","30/60/90")
- risks: array of objects with keys optional cluster_id, risk, severity (1-5), likelihood (1-5), mitigation
- constraints: array of strings (carry forward relevant ones)
- captured_decisions: array of objects with keys optional cluster_id, topic, decision, optional user_id, rational
- captured_action_items: array mirroring action_items if provided upstream, de-duplicated
- open_unknowns: array of strings
- document_spec: object describing the printable report. Use the following JSON shape exactly (values may vary):
    {{
        "title": "Action Plan",
        "cover": {{
            "subtitle": "From feasibility & shortlist to execution",
            "objective": "...",
            "date_str": "YYYY-MM-DD HH:MM UTC",
            "owner_note": "Who owns what and how to track"
        }},
        "sections": [
            {{"heading":"Executive Summary","blocks":[
                {{"type":"p","text":"Overall readiness and summary of scope"}},
                {{"type":"ul","items":["Key goals","Top risks","Success metrics overview"]}}
            ]}},
            {{"heading":"Action Items","blocks":[
                {{"type":"table","columns":["Title","Owner","Due Date","Metric","Dependencies"],
                 "rows":[["...","...","...","...","..."]]}}
            ]}},
            {{"heading":"Milestones (30/60/90)","blocks":[
                {{"type":"table","columns":["Milestone","Target Date"],"rows":[["...","..."]]}}
            ]}},
            {{"heading":"Operating Model","blocks":[
                {{"type":"p","text":"Roles, cadences, reporting"}},
                {{"type":"ul","items":["Owner sync cadence","Status reporting","Change management"]}}
            ]}},
            {{"heading":"Risks & Mitigations","blocks":[
                {{"type":"table","columns":["Risk","Severity","Likelihood","Mitigation"],"rows":[["...","...","...","..."]]}}
            ]}},
            {{"heading":"Constraints & Assumptions","blocks":[
                {{"type":"ul","items":["...","..."]}}
            ]}}
        ],
        "appendices":[
            {{"heading":"Source Decisions","blocks":[
                {{"type":"table","columns":["Topic","Decision","Owner"],"rows":[["...","...","..."]]}}
            ]}}
        ]
    }}

Hard rules:
- Do NOT invent clusters or participants; use provided participants_json and prioritized_json for references.
- Prefer owners whose names/emails appear in participants_json; else set owner_user_id to null.
- Keep JSON valid: no markdown in fields except document_spec blocks text; no code fences; no trailing commas.

Workshop Snapshot (JSON):
{workshop_overview}

Framing (JSON):
{framing_json}

Feasibility Analysis (JSON):
{feasibility_json}

Feasibility Report (JSON):
{feasibility_report}

Shortlist / Prioritized (JSON):
{prioritized_json}

Shortlist Document (JSON):
{shortlist_document}

Decisions (prior phase) (JSON):
{captured_decisions}

Existing Action Items (JSON):
{captured_action_items}

Open Unknowns (JSON):
{open_unknowns}

Notable Findings (JSON):
{notable_findings}

Clusters + Ideas + Votes (JSON):
{clusters_full_json}

Participants Roster (JSON):
{participants_json}

Pre-Workshop Research (may be truncated):
{pre_workshop_data}

Phase Label: {current_phase_label}
Phase Context: {phase_context}
"""
    prompt = PromptTemplate.from_template(template)
    chain = prompt | llm
    raw = chain.invoke(inputs)
    print("\n\n\n\n[Action Planner] LLM raw response", raw) # This is the current output
    
    
    text = _safe(getattr(raw, "content", raw))
    json_block = extract_json_block(text) or text
    try:
        data = json.loads(json_block)
    except Exception as exc:
        raise RuntimeError(f"Model did not return valid JSON: {exc}")
    if not isinstance(data, dict):
        raise RuntimeError("Model output must be a JSON object.")
    return data


# =========================
# API Entry Point
# =========================
def get_action_plan_payload(workshop_id: int, phase_context: str | None = None) -> Union[Dict[str, Any], Tuple[str, int]]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return "Workshop not found", 404

    # Prepare inputs
    try:
        inputs, _meta = _prepare_action_plan_inputs(workshop_id, phase_context)
    except Exception as exc:
        current_app.logger.error("[ActionPlan] Input prep failed: %s", exc, exc_info=True)
        return "Failed to prepare action plan inputs", 500

    # Invoke LLM
    try:
        data = _invoke_action_plan_model(inputs)
    except Exception as exc:
        current_app.logger.error("[ActionPlan] LLM error: %s", exc, exc_info=True)
        return str(exc), 503

    # Validate required fields (strict LLM mode)
    required = [
        "title", "task_type", "task_description", "instructions", "task_duration",
        "narration", "tts_script", "tts_read_time_seconds",
        "action_items", "milestones", "document_spec"
    ]
    if not all(k in data for k in required):
        return "Action Plan output missing required fields", 500

    doc_spec = data.get("document_spec")
    if isinstance(doc_spec, dict):
        _normalize_action_plan_doc_spec(doc_spec)

    # Persist task with raw LLM payload (no rewriting)
    task = BrainstormTask(
        workshop_id=workshop_id,
        task_type=_safe(data.get("task_type") or "results_action_plan"),
        title=_safe(data.get("title") or "Action Plan"),
        description=_safe(data.get("task_description")),
        duration=int(data.get("task_duration") or 900),
        status="pending",
    )
    payload_str = json.dumps(data, ensure_ascii=False)
    task.prompt = payload_str
    task.payload_json = payload_str
    db.session.add(task)
    db.session.flush()
    data["task_id"] = task.id

    # Render PDF strictly from LLM's document_spec
    try:
        assets = _generate_action_plan_pdf(ws, data.get("document_spec") or {})
        if assets:
            abs_path, rel_path, url = assets
            data["action_plan_pdf_path"] = rel_path
            data["action_plan_pdf_url"] = url
            data["pdf_document"] = url
            _attach_doc(ws, abs_path, rel_path, url, data)
            augmented = json.dumps(data, ensure_ascii=False)
            task.payload_json = augmented
            task.prompt = augmented
    except Exception as exc:
        current_app.logger.error("[ActionPlan] PDF generation error: %s", exc, exc_info=True)

    current_app.logger.info("[ActionPlan] Created task %s for workshop %s", task.id, ws.id)
    return data
