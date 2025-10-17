# app/service/routes/prioritization.py
from __future__ import annotations

import os, json
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.config import Config, TASK_SEQUENCE
from app.models import (
    Workshop,
    BrainstormTask,
    IdeaCluster,
    IdeaVote,
    BrainstormIdea,
    Document,
    WorkshopDocument,
    WorkshopPlanItem,
)
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.llm_bedrock import get_chat_llm
from langchain_core.prompts import PromptTemplate

# ---------- shared PDF primitives (same style as feasibility JSON->PDF) ----------
def _reports_dir() -> str:
    base = os.path.join(current_app.instance_path, "uploads", "reports")
    os.makedirs(base, exist_ok=True)
    return base

def _safe_title(s: str) -> str:
    return (s or "Workshop").strip().replace("/", "-")

def _render_pdf_from_doc_spec(workshop: Workshop, doc_spec: Dict[str, Any]) -> Tuple[str, str, str] | None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle, ListFlowable, ListItem, PageBreak
        from reportlab.pdfgen import canvas
    except Exception as exc:
        current_app.logger.warning("[Prioritization] ReportLab unavailable: %s", exc)
        return None

    ts = datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S")
    fname = f"{_safe_title(workshop.title)} shortlist {ts}.pdf"
    abs_path = os.path.join(_reports_dir(), fname)
    rel_path = os.path.join("uploads", "reports", fname)

    base = getSampleStyleSheet()
    Title = ParagraphStyle("BX_Title", parent=base["Title"], fontSize=24, leading=28, alignment=TA_LEFT, spaceBefore=18, spaceAfter=10, textColor=colors.HexColor("#111827"))
    H1 = ParagraphStyle("BX_H1", parent=base["Heading2"], fontSize=16, leading=20, spaceBefore=16, spaceAfter=8, textColor=colors.HexColor("#111827"))
    H2 = ParagraphStyle("BX_H2", parent=base["Heading3"], fontSize=13, leading=17, spaceBefore=10, spaceAfter=6, textColor=colors.HexColor("#111827"))
    Body = ParagraphStyle("BX_Body", parent=base["BodyText"], fontSize=10.5, leading=14.0, textColor=colors.HexColor("#111827"), spaceAfter=6)
    Note = ParagraphStyle("BX_Note", parent=base["BodyText"], fontSize=9.5, leading=13, textColor=colors.HexColor("#6B7280"), spaceBefore=4, spaceAfter=6)

    def rule(space=8, color="#E5E7EB"):
        t = Table([[""]], colWidths=[6.5 * inch], rowHeights=[0.7])
        t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), colors.HexColor(color))]))
        return [Spacer(1, space), t, Spacer(1, space)]

    def _footer(canv: canvas.Canvas, doc):
        canv.setFont("Helvetica", 9)
        canv.setFillColor(colors.HexColor("#6B7280"))
        canv.drawRightString(7.95 * inch, 0.5 * inch, f"Page {doc.page}")

    frame = Frame(0.75 * inch, 0.75 * inch, 7.0 * inch, 9.75 * inch, showBoundary=0)
    doc = BaseDocTemplate(abs_path, pagesize=LETTER, leftMargin=0, rightMargin=0, topMargin=0, bottomMargin=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_footer)])

    def _ul(items: List[str]) -> ListFlowable:
        return ListFlowable([ListItem(Paragraph(str(x), Body)) for x in items or []], bulletType="bullet", leftIndent=12)

    def _table(columns: List[str], rows: List[List[str]]) -> Table:
        data = [[Paragraph(str(c), Body) for c in (columns or [])]]
        for r in rows or []:
            data.append([Paragraph(str(c), Body) for c in r])
        t = Table(data, colWidths=[(6.5 * inch) / max(1, len(columns or [""]))] * max(1, len(columns or [""])))
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#F3F4F6")),
            ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#D1D5DB")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        return t

    E: List[Any] = []
    E.append(Paragraph(doc_spec.get("title") or f"{workshop.title} — Prioritized Shortlist", Title))
    E += rule(color="#0d6efd")

    cover = doc_spec.get("cover") or {}
    if cover.get("summary"):
        E.append(Paragraph("Executive Summary", H2))
        E.append(Paragraph(cover["summary"], Body))
    if cover.get("date_str"):
        E.append(Paragraph("Date", H2))
        E.append(Paragraph(str(cover["date_str"]), Body))
    if cover.get("weights"):
        E.append(Paragraph("Weighting", H2))
        wt = cover["weights"]
        E.append(_table(["Metric", "Weight"], [[k, str(v)] for k,v in wt.items()]))

    E.append(PageBreak())

    for sec in doc_spec.get("sections", []):
        if sec.get("heading"):
            E.append(Paragraph(sec["heading"], H1))
        for blk in sec.get("blocks", []):
            t = (blk.get("type") or "p").lower()
            if t == "p":
                E.append(Paragraph(str(blk.get("text") or ""), Body))
            elif t == "h2":
                E.append(Paragraph(str(blk.get("text") or ""), H2))
            elif t == "ul":
                E.append(_ul(list(blk.get("items") or [])))
            elif t == "table":
                E.append(_table(blk.get("columns") or [], blk.get("rows") or []))
            elif t == "rule":
                E += rule()
            elif t == "page_break":
                E.append(PageBreak())

    E += rule()
    E.append(Paragraph(f"Prepared by BrainStormX • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", Note))

    doc.build(E)
    url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{os.path.basename(rel_path)}"
    return abs_path, rel_path, url

def _attach_doc(ws: Workshop, abs_path: str, rel_path: str, url: str, payload: Dict[str, Any]) -> None:
    try:
        size = os.path.getsize(abs_path) if os.path.exists(abs_path) else None
        d = Document(
            workspace_id=ws.workspace_id,
            title=f"{ws.title} — Shortlist Brief",
            description="Auto-generated prioritized shortlist",
            file_name=os.path.basename(rel_path),
            file_path=rel_path,
            uploaded_by_id=ws.created_by_id,
            file_size=size,
        )
        db.session.add(d); db.session.flush()
        link = WorkshopDocument(workshop_id=ws.id, document_id=d.id)
        db.session.add(link); db.session.flush()
        doc_payload = {
            "id": d.id, "title": d.title, "file_name": d.file_name, "file_size": d.file_size,
            "file_path": rel_path, "url": url, "workshop_link_id": link.id,
        }
        payload["shortlist_document"] = dict(doc_payload)
    except Exception as exc:
        current_app.logger.warning("[Prioritization] Could not attach document: %s", exc)

# ---------- input assembly ----------
def _collect_overview(ws: Workshop) -> Dict[str, Any]:
    try:
        pc = ws.participants.count() if hasattr(ws.participants, "count") else len(list(ws.participants))  # type: ignore[arg-type]
    except Exception:
        pc = 0
    return {
        "title": ws.title,
        "objective": ws.objective or "TBD",
        "scheduled_for": ws.date_time.strftime("%Y-%m-%d %H:%M UTC") if ws.date_time else "unscheduled",
        "participant_count": pc,
        "status": ws.status,
    }

def _load_latest_payload(workshop_id: int, types: List[str]) -> Optional[Dict[str, Any]]:
    t = (
        BrainstormTask.query
        .filter(BrainstormTask.workshop_id == workshop_id, BrainstormTask.task_type.in_(types))
        .order_by(BrainstormTask.created_at.desc())
        .first()
    )
    if not t or not t.payload_json:
        return None
    try:
        data = json.loads(t.payload_json)
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _clusters_with_votes(previous_task_id: int) -> List[Dict[str, Any]]:
    rows = (
        db.session.query(
            IdeaCluster.id.label("cluster_id"),
            IdeaCluster.name,
            IdeaCluster.description,
            func.count(IdeaVote.id).label("votes"),
        )
        .outerjoin(IdeaVote, IdeaCluster.id == IdeaVote.cluster_id)
        .filter(IdeaCluster.task_id == previous_task_id)
        .group_by(IdeaCluster.id)
        .order_by(func.count(IdeaVote.id).desc(), IdeaCluster.id.asc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        ideas = BrainstormIdea.query.filter_by(cluster_id=r.cluster_id).order_by(BrainstormIdea.id.asc()).all()
        out.append({
            "cluster_id": int(r.cluster_id),
            "title": r.name or f"Cluster {r.cluster_id}",
            "description": r.description or "",
            "vote_count": int(r.votes or 0),
            "ideas": [{"idea_id": int(i.id), "text": (i.corrected_text or i.content or "").strip()} for i in ideas],
        })
    return out

def _prepare_inputs(workshop_id: int, previous_task_id: int, phase_context: str | None) -> Dict[str, Any]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws: raise RuntimeError("Workshop not found")

    overview = _collect_overview(ws)
    framing = _load_latest_payload(workshop_id, ["framing"]) or {}
    brainstorming = _load_latest_payload(workshop_id, ["brainstorming", "ideas"]) or {}
    clustering_voting = _load_latest_payload(workshop_id, ["clustering_voting"]) or {}
    feasibility = _load_latest_payload(workshop_id, ["results_feasibility"]) or {}

    rubrics = feasibility.get("feasibility_rules_and_rubrics") or _load_latest_payload(workshop_id, ["framing"]) or {}
    clusters = _clusters_with_votes(previous_task_id)

    prework = get_pre_workshop_context_json(workshop_id)

    next_phase = {"task_type": "results_action_plan", "estimated_duration": 1200}

    return {
        "workshop_overview": json.dumps(overview, ensure_ascii=False, indent=2),
        "framing_json": json.dumps({
            "problem_statement": framing.get("problem_statement"),
            "success_criteria": framing.get("success_criteria"),
            "context_summary": framing.get("context_summary"),
            "constraints": framing.get("constraints"),
            "assumptions": framing.get("assumptions"),
        }, ensure_ascii=False, indent=2),
        "brainstorming_json": json.dumps(brainstorming, ensure_ascii=False, indent=2),
        "current_phase_label": phase_context or "Prioritization & Shortlist",
        "phase_context": phase_context or "Turn feasibility and votes into a defensible shortlist.",
        "clustering_voting_json": json.dumps(clustering_voting, ensure_ascii=False, indent=2),
        "clusters_full_json": json.dumps(clusters, ensure_ascii=False, indent=2),
        "feasibility_json": json.dumps({
            "feasibility_analysis": feasibility.get("analysis"),
            "feasibility_report": feasibility.get("document_spec"),
            "narration": feasibility.get("narration"),
        }, ensure_ascii=False, indent=2),
        "feasibility_analysis": json.dumps(feasibility.get("analysis"), ensure_ascii=False, indent=2),
        "feasibility_report": json.dumps(feasibility.get("document_spec"), ensure_ascii=False, indent=2),
        "feasibility_rules_and_rubrics": json.dumps(rubrics, ensure_ascii=False, indent=2),
        "pre_workshop_data": prework,
        "next_phase_json": json.dumps(next_phase, ensure_ascii=False, indent=2),
    }

# ---------- LLM call ----------
def _invoke_llm(inputs: Dict[str, Any]) -> Dict[str, Any]:
    llm = get_chat_llm(model_kwargs={"temperature": 0.35, "max_tokens": 2200, "top_p": 0.9})
    template = """
You are the Prioritization Composer. Using ONLY provided context, produce a single strict JSON object.

Contract (top-level keys are REQUIRED):
- title: "Prioritization & Shortlisting"
- task_type: "results_prioritization"
- task_description: one sentence for participants.
- instructions: one paragraph of guidance (plain text).
- task_duration: integer seconds.
- narration: one short paragraph for the facilitator to frame the shortlist.
- tts_script: one paragraph (natural TTS).
- tts_read_time_seconds: integer >= 45.
- weights: object with numeric weights used by the scorer (e.g., impact, reach, confidence, effort, feasibility, strategic_fit).
- methods: array of method names used (e.g., "RICE","ICE","Kano","Impact-Effort").
- prioritized: array of clusters with:
    {
      "cluster_id": number,
      "title": string,
      "description": string,
      "vote_count": number,
      "rank": number,
      "scores": {
        "RICE": number,
        "ICE": number,
        "Kano": number,
        "Impact-Effort": "High Impact/Low Effort" | "High Impact/High Effort" | "Low Impact/Low Effort" | "Low Impact/High Effort",
        "impact": number, "reach": number, "confidence": number, "effort": number,
        "feasibility": number, "strategic_fit": number, "success_criteria_alignment": number
      },
      "position": string (same label as Impact-Effort),
      "kano_type": "Basic" | "Performance" | "Excitement" | "Indifferent" | "Reverse" | "TBD",
      "why": string,
      "representative_ideas": [{"idea_id": number, "text": string}],
      "risks": [{"risk": string, "severity": 1-5, "likelihood": 1-5, "mitigation": string}]
    }
- constraints: array of strings (carry forward any known constraints; use "TBD" if unknown).
- captured_decisions: array of {cluster_id, topic, decision, user_id: number|null, rational}
- captured_action_items: array of {title, user_id: number|null, metric, cluster_id}
- open_unknowns: array of strings.
- notable_findings: array of strings.
- document_spec: object for PDF renderer with:
    {
      "title": "Prioritized Ideas – Workshop Shortlist",
      "cover": {
        "summary": string executive summary,
        "date_str": string,
        "weights": {"impact":0.4,...}
      },
      "sections": [
        {
          "heading": "Shortlist Overview",
          "blocks":[
            {"type":"table","columns":["Rank","Cluster","Votes","RICE","ICE","Impact","Effort","Feasibility","Strategic Fit"],"rows":[ ... ]},
            {"type":"note","text":"Methods used: ..."}
          ]
        },
        {
          "heading": "Top Cluster Details",
          "blocks":[
            {"type":"h2","text":"<Cluster Title>"},
            {"type":"p","text":"Why it ranks here..."},
            {"type":"table","columns":["Metric","Score"],"rows":[["Impact","9"],["Effort","3"], ...]},
            {"type":"ul","items":["Key risk ...","Mitigation ..."]},
            {"type":"rule"}
          ]
        }
      ]
    }

Hard rules:
- Use ONLY clusters from Clusters JSON. No new clusters.
- If a detail is unknown, write "TBD".
- Valid JSON only. No markdown. No code fences.

Workshop Snapshot:
{workshop_overview}

Framing Highlights:
{framing_json}

Brainstorming Summary:
{brainstorming_json}

Phase Label: {current_phase_label}
Phase Context:
{phase_context}

Clustering & Voting:
{clustering_voting_json}

Clusters (ideas, votes):
{clusters_full_json}

Feasibility Summary:
{feasibility_json}

Feasibility Analysis:
{feasibility_analysis}

Feasibility Report:
{feasibility_report}

Rubrics & Rules:
{feasibility_rules_and_rubrics}

Upcoming Phase:
{next_phase_json}
"""
    prompt = PromptTemplate.from_template(template)
    raw = (prompt | llm).invoke(inputs)
    txt = raw.content if hasattr(raw, "content") else str(raw)
    block = extract_json_block(txt) or txt
    return json.loads(block)

# ---------- API entry ----------
def get_prioritization_payload(workshop_id: int, previous_task_id: int, phase_context: str) -> Dict[str, Any] | Tuple[str, int]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws: return "Workshop not found", 404

    try:
        inputs = _prepare_inputs(workshop_id, previous_task_id, phase_context)
    except Exception as exc:
        current_app.logger.error("[Prioritization] Input error: %s", exc, exc_info=True)
        return "Failed to prepare prioritization inputs", 500

    try:
        data = _invoke_llm(inputs)
    except Exception as exc:
        current_app.logger.error("[Prioritization] LLM error: %s", exc, exc_info=True)
        return "Prioritization generation error", 503

    required = ["title","task_type","task_description","instructions","task_duration","narration","tts_script","tts_read_time_seconds","prioritized","weights","methods","document_spec"]
    if not all(k in data for k in required):
        return "Prioritization output missing required fields", 500

    # Persist task (LLM content only)
    task = BrainstormTask(
        workshop_id=workshop_id,
        task_type=str(data.get("task_type")),
        title=str(data.get("title")),
        description=str(data.get("task_description") or ""),
        duration=int(data.get("task_duration") or 900),
        status="pending",
        prompt=json.dumps(data, ensure_ascii=False),
        payload_json=json.dumps(data, ensure_ascii=False),
    )
    db.session.add(task); db.session.flush()
    data["task_id"] = task.id

    # PDF
    try:
        assets = _render_pdf_from_doc_spec(ws, data.get("document_spec") or {})
        if assets:
            abs_path, rel_path, url = assets
            data["shortlist_pdf_path"] = rel_path
            data["shortlist_pdf_url"] = url
            _attach_doc(ws, abs_path, rel_path, url, data)
            augmented = json.dumps(data, ensure_ascii=False)
            task.payload_json = augmented
            task.prompt = augmented
    except Exception as exc:
        current_app.logger.error("[Prioritization] PDF error: %s", exc, exc_info=True)

    current_app.logger.info("[Prioritization] Created task %s for workshop %s", task.id, ws.id)
    return data
