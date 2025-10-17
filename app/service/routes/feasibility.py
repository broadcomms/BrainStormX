# app/service/routes/feasibility.py
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from flask import current_app
from sqlalchemy import func


from app.config import Config
from app.extensions import db
from app.models import (
    Workshop,
    BrainstormTask,
    IdeaCluster,
    IdeaVote,
    Document,
    WorkshopDocument,
    BrainstormIdea,
    WorkshopPlanItem,
)

from app.utils.agenda_utils import strip_agenda_durations
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.llm_bedrock import get_chat_llm, get_chat_llm_pro

from langchain_core.prompts import PromptTemplate

# =========================
# Errors
# =========================
class FeasibilityGenerationError(RuntimeError):
    """Raised when the feasibility payload cannot be generated from the LLM output."""
    
# =========================
# Small utils
# =========================
def _safe(s: Any) -> str:
    return (str(s) if s is not None else "").strip()

def _coerce_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        if "content" in val and isinstance(val["content"], str):
            return val["content"]
        return json.dumps(val, ensure_ascii=False)
    if hasattr(val, "content"):
        c = getattr(val, "content")
        if isinstance(c, str):
            return c
    return str(val)

def _reports_dir() -> str:
    base = os.path.join(current_app.instance_path, "uploads", "reports")
    os.makedirs(base, exist_ok=True)
    return base

def _safe_title(s: str) -> str:
    return (s or "Workshop").strip().replace("/", "-")


def _ensure_feasibility_contract(payload: Dict[str, Any]) -> None:
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        analysis = {}
        payload["analysis"] = analysis

    clusters = analysis.get("clusters")
    if not isinstance(clusters, list):
        clusters = []
    analysis["clusters"] = [c for c in clusters if isinstance(c, dict)]
    analysis.setdefault("method_notes", "")

    doc_spec = payload.get("document_spec")
    if not isinstance(doc_spec, dict):
        doc_spec = {}
        payload["document_spec"] = doc_spec

    doc_spec.setdefault("title", payload.get("title") or "Feasibility Analysis")
    cover = doc_spec.get("cover")
    if not isinstance(cover, dict):
        cover = {}
        doc_spec["cover"] = cover
    cover.setdefault("subtitle", "Feasibility Assessment Overview")
    cover.setdefault("objective", payload.get("task_description") or "")
    cover.setdefault("date_str", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    top_clusters = cover.get("top_clusters")
    if not isinstance(top_clusters, list) or not top_clusters:
        top_clusters = []
        for cluster in analysis["clusters"][:3]:
            top_clusters.append(
                {
                    "name": _safe(cluster.get("cluster_name") or cluster.get("name") or "TBD"),
                    "votes": int(cluster.get("votes") or 0),
                }
            )
        if not top_clusters:
            top_clusters = [{"name": "TBD", "votes": 0}]
        cover["top_clusters"] = top_clusters

    sections = doc_spec.get("sections")
    if not isinstance(sections, list):
        sections = []

    if not sections:
        summary_text = payload.get("narration") or payload.get("instructions") or "Feasibility summary pending."
        clusters_rows = []
        for cluster in analysis["clusters"]:
            clusters_rows.append([
                _safe(cluster.get("cluster_name") or cluster.get("name") or "TBD"),
                str(cluster.get("votes") or 0),
            ])
        if not clusters_rows:
            clusters_rows = [["TBD", "0"]]

        sections = [
            {
                "heading": "Executive Summary",
                "blocks": [
                    {"type": "p", "text": summary_text},
                ],
            },
            {
                "heading": "Top Clusters",
                "blocks": [
                    {
                        "type": "table",
                        "columns": ["Top Clusters", "Votes"],
                        "rows": clusters_rows,
                    }
                ],
            },
        ]

    doc_spec["sections"] = [s for s in sections if isinstance(s, dict)]

    appendices = doc_spec.get("appendices")
    if appendices is None:
        doc_spec["appendices"] = []
    elif isinstance(appendices, list):
        doc_spec["appendices"] = [a for a in appendices if isinstance(a, dict)]
    else:
        doc_spec["appendices"] = []


# =========================
# PDF (ReportLab) – JSON renderer
# =========================
def _generate_feasibility_pdf(
    workshop: Workshop,
    doc_spec: Dict[str, Any],
) -> Tuple[str, str, str] | None:
    """
    Render a professional feasibility report using a structured JSON 'doc_spec':
    The LLM owns the content; this function only lays it out.
    Supported block types: p, h2, ul, table, note, rule, page_break

    Expected schema (LLM-owned; do not second-guess or fallback):
    {
      "title": "Feasibility Analysis",
      "cover": {
        "subtitle": "...",
        "objective": "...",
        "date_str": "YYYY-MM-DD HH:MM UTC",
        "top_clusters": [{"name": "...","votes": 0}, ...]
      },
      "sections": [
        {
          "heading": "Executive Summary",
          "blocks": [
            {"type":"p","text":"..."},
            {"type":"ul","items":["...","..."]},
            {"type":"table","columns":["Col A","Col B"], "rows":[["a","b"], ...]},
            {"type":"note","text":"..."}
          ]
        },
        ...
      ],
      "appendices": [...]
    }
    """
    try:
        from reportlab.lib import colors  # type: ignore
        from reportlab.lib.enums import TA_LEFT  # type: ignore
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore
        from reportlab.lib.units import inch  # type: ignore
        from reportlab.platypus import (  # type: ignore
            BaseDocTemplate,
            Frame,
            PageTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
            ListFlowable,
            ListItem,
            PageBreak,
        )
        from reportlab.pdfgen import canvas  # type: ignore
        from reportlab.platypus.flowables import HRFlowable
    except Exception as exc:
        current_app.logger.warning("[Feasibility] ReportLab unavailable: %s", exc)
        return None

    # Paths
    ts = datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S")
    fname = f"{_safe_title(workshop.title)} feasibility {ts}.pdf"
    abs_path = os.path.join(_reports_dir(), fname)
    rel_path = os.path.join("uploads", "reports", fname)

    # Styles
    base = getSampleStyleSheet()
    Title = ParagraphStyle(
        "BX_Title",
        parent=base["Title"],
        fontSize=24,
        leading=24,
        alignment=TA_LEFT,
        spaceBefore=14,
        spaceAfter=8,
    )
    Subhead = ParagraphStyle(
        "BX_Subhead",
        parent=base["Heading2"],
        fontSize=12,
        leading=14,
        textColor=colors.HexColor("#4B5563"),  # neutral-600
        spaceAfter=10,
    )
    Sub = ParagraphStyle(
        "BX_Sub",
        parent=base["Heading2"],
        fontSize=12,
        leading=14,
        textColor=colors.HexColor("#4B5563"),  # neutral-600
        spaceAfter=10,
    )
    H1 = ParagraphStyle(
        "BX_H1",
        parent=base["Heading2"],
        fontSize=14,
        leading=17,
        textColor=colors.HexColor("#111827"),  # neutral-900
        spaceBefore=10,
        spaceAfter=6,
    )
    H2 = ParagraphStyle(
        "BX_H2",
        parent=base["Heading3"],
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#111827"),
        spaceBefore=8,
        spaceAfter=4,
    )
    Body = ParagraphStyle(
        "BX_Body",
        parent=base["BodyText"],
        fontSize=10.5,
        leading=14,
        spaceAfter=6,
    )
    Note = ParagraphStyle(
        "BX_Note",
        parent=base["BodyText"],
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#6B7280"),
        spaceBefore=4,
        spaceAfter=6,
    )
    Small = ParagraphStyle(
        "BX_Small",
        parent=base["BodyText"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#6B7280"),  # neutral-500
        spaceAfter=4,
    )
    Quote = ParagraphStyle(
        "BX_Quote",
        parent=base["BodyText"],
        fontSize=11,
        leading=16,
        leftIndent=0,
        textColor=colors.HexColor("#111827"),
        spaceBefore=4,
        spaceAfter=8,
    )
    def rule(width=6.5 * inch, height=0.7, color="#E5E7EB", space=8):
        t = Table([[""]], colWidths=[width], rowHeights=[height])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(color))]))
        return [Spacer(1, space), t, Spacer(1, space)]
    PAGE_W, PAGE_H = LETTER
    LEFT_MARGIN, RIGHT_MARGIN = 0.75*inch, 0.75*inch
    TOP_MARGIN, BOTTOM_MARGIN = 1.25*inch, 0.75*inch
    usable_w = PAGE_W - LEFT_MARGIN - RIGHT_MARGIN
    frame = Frame(LEFT_MARGIN, BOTTOM_MARGIN, usable_w, PAGE_H - TOP_MARGIN - BOTTOM_MARGIN, showBoundary=0)
    
    def rule_2(usable_w, height=0.6, color="#E5E7EB", space=8):
        t = Table([[""]], colWidths=[usable_w], rowHeights=[height])
        t.hAlign = "LEFT"
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor(color)),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ("TOPPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        return [Spacer(1, space), t, Spacer(1, space)]

    def bullet_list(items: List[str]) -> ListFlowable:
        cleaned = [Paragraph(i, Body) for i in items if i]
        return ListFlowable(cleaned, bulletType="bullet", start=None, leftIndent=12)

    def chip_row(items: List[str]) -> Table:
        """Simple chip-like pills using tables."""
        chips = []
        for txt in items[:8]:  # avoid overflow
            cell = Paragraph(txt, ParagraphStyle("chip", parent=Small, textColor=colors.HexColor("#111827")))
            chips.append([cell])
        # Arrange chips into columns
        cols = 3
        rows = []
        for i in range(0, len(chips), cols):
            rows.append([c[0] for c in chips[i : i + cols]])
        if not rows:
            rows = [[""]]

        tbl = Table(rows, colWidths=[2.1 * inch] * min(cols, len(rows[0])))
        tbl.hAlign = "LEFT"
        tbl.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0,0), (-1,-1), 0),
                    ("RIGHTPADDING", (0,0), (-1,-1), 0),
                    ("TOPPADDING", (0,0), (-1,-1), 0),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                    ("BOX", (0, 0), (-1, -1), 0, colors.white),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),  # neutral-100
                    ("INNERGRID", (0, 0), (-1, -1), 1, colors.white),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ]
            )
        )
        return tbl

    def meta_value(v: Any, fallback: str = "—") -> str:
        return str(v) if (v is not None and str(v).strip()) else fallback

    # Footer
    def _footer(canv: canvas.Canvas, doc):
        canv.setFont("Helvetica", 9)
        canv.setFillColor(colors.HexColor("#6B7280"))
        canv.drawRightString(7.95 * inch, 0.5 * inch, f"Page {doc.page}")

    frame = Frame(0.75 * inch, 0.75 * inch, 7.0 * inch, 9.75 * inch, showBoundary=0)
    doc = BaseDocTemplate(abs_path, pagesize=LETTER, leftMargin=0, rightMargin=0, topMargin=0, bottomMargin=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_footer)])

    def _ul(items: List[Any]) -> ListFlowable:
        entries: List[ListItem] = []
        iterable = items if isinstance(items, list) else []
        for item in iterable:
            entries.append(ListItem(Paragraph(_safe(item), Body)))
        return ListFlowable(cast(List[Any], entries), bulletType="bullet", leftIndent=12)

    def _table(columns: Any, rows: Any) -> Table:
        header = columns if isinstance(columns, list) else []
        body_rows = rows if isinstance(rows, list) else []

        data: List[List[Paragraph]] = []
        if header:
            data.append([Paragraph(_safe(col), Body) for col in header])
        for row in body_rows:
            if isinstance(row, list):
                data.append([Paragraph(_safe(cell), Body) for cell in row])

        if not data:
            data = [[Paragraph("", Body)]]
        column_count = len(data[0]) if data and data[0] else 1
        tbl = Table(data, colWidths=[(6.5 * inch) / column_count] * column_count)
        tbl.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0,0), (-1,-1), -10),
                    ("RIGHTPADDING", (0,0), (-1,-1), 0),
                    ("TOPPADDING", (0,0), (-1,-1), 0),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                    ("BOX", (0, 0), (-1, -1), 0, colors.white),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),  # neutral-100
                    ("INNERGRID", (0, 0), (-1, -1), 1, colors.white),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ]
            )
        )
        return tbl

    def _render_blocks(blocks: Any) -> None:
        if not isinstance(blocks, list):
            return
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            btype = _safe(blk.get("type") or "p").lower()
            if btype == "p":
                content = blk.get("content") if "content" in blk else blk.get("text")
                E.append(Paragraph(_safe(content), Body))
            elif btype in {"list", "ul", "unordered_list"}:
                raw_items = blk.get("items")
                items_list: List[Any] = raw_items if isinstance(raw_items, list) else []
                E.append(_ul(items_list))
            elif btype == "h2":
                content = blk.get("content") if "content" in blk else blk.get("text")
                E.append(Paragraph(_safe(content), H2))
            elif btype == "note":
                note_text = blk.get("content") if "content" in blk else blk.get("text")
                E.append(Paragraph(_safe(note_text), Note))
            elif btype == "table":
                E.append(_table(blk.get("columns"), blk.get("rows")))
            elif btype == "rule":
                E.extend(rule())
            elif btype == "page_break":
                E.append(PageBreak())
            else:
                fallback_text = blk.get("content") if "content" in blk else blk.get("text")
                E.append(Paragraph(_safe(fallback_text), Body))

    # Build
    E: List[Any] = []

    # Cover
    cover_raw = doc_spec.get("cover")
    cover: Dict[str, Any] = cover_raw if isinstance(cover_raw, dict) else {}
    E.append(Spacer(1, 14))
    title_text = _safe(doc_spec.get("title"))
    E.append(Paragraph(title_text, Title))
    E += rule_2(usable_w,color="#0d6efd")
    
    subtitle = _safe(cover.get("subtitle"))
    if ("subtitle" in cover) or subtitle:
        E.append(Paragraph(subtitle, Sub))
        

    if "date_str" in cover:
        date_str = _safe(cover.get("date_str"))
    else:
        date_str = "TBD"
    meta_items = [
        f"<b>Date</b> {date_str}",
    ]
    for line in meta_items:
        E.append(Paragraph(line, Small))
    E.append(Spacer(1, 10))



    E += rule_2(usable_w)

    # Meta
    if "objective" in cover:
        objective = _safe(cover.get("objective"))
        E.append(Paragraph("Objective", H2))
        E.append(Paragraph(objective, Body))
        E.append(Spacer(1, 10))


    top_clusters_raw = cover.get("top_clusters")
    top_clusters: List[Any] = top_clusters_raw if isinstance(top_clusters_raw, list) else []
    cluster_rows: List[List[str]] = []
    for cluster in top_clusters:
        if not isinstance(cluster, dict):
            continue
        name = _safe(cluster.get("name"))
        votes_val = cluster.get("votes")
        votes_str = "" if votes_val is None else str(votes_val)
        cluster_rows.append([name, votes_str])
    if cluster_rows:
        E.append(Paragraph("Top Voted Clusters", H2))
        E.append(_table(["Cluster", "Votes"], cluster_rows))
    E.append(PageBreak())

    # Sections
    sections_raw = doc_spec.get("sections")
    sections: List[Any] = sections_raw if isinstance(sections_raw, list) else []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        heading = _safe(sec.get("heading"))
        if heading:
            E += rule_2(usable_w)
            E.append(Paragraph(heading, H1)) 
        _render_blocks(sec.get("blocks"))

    appendices_raw = doc_spec.get("appendices")
    appendices: List[Any] = appendices_raw if isinstance(appendices_raw, list) else []
    valid_appendices = [ap for ap in appendices if isinstance(ap, dict)]
    if valid_appendices:
        E.append(PageBreak())
        for appendix in valid_appendices:
            heading = _safe(appendix.get("heading"))
            if heading:
                E.append(Paragraph(heading, H1))
            _render_blocks(appendix.get("blocks"))

    # Footer meta
    E += rule_2(usable_w)
    prepared = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    E.append(Paragraph(f"Prepared by BrainStormX • {prepared}", Note))

    try:
        doc.build(E)
    except Exception as exc:
        current_app.logger.error("[Feasibility] PDF build failed: %s", exc, exc_info=True)
        return None

    url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{os.path.basename(rel_path)}"
    return abs_path, rel_path, url


def _attach_doc(ws: Workshop, abs_path: str, rel_path: str, url: str, payload: Dict[str, Any]) -> None:
    """Persist Document + link to workshop and mirror details into payload."""
    try:
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = None

        title = f"{ws.title} — Feasibility Report"
        doc = Document()
        doc.workspace_id = ws.workspace_id
        doc.title = title
        doc.description = "Automatically generated feasibility report"
        doc.file_name = os.path.basename(rel_path)
        doc.file_path = rel_path
        doc.uploaded_by_id = ws.created_by_id
        doc.file_size = size
        db.session.add(doc)
        db.session.flush()

        link = WorkshopDocument()
        link.workshop_id = ws.id
        link.document_id = doc.id
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
        payload["feasibility_document"] = dict(doc_payload)
        payload["document"] = dict(doc_payload)
    except Exception as exc:
        current_app.logger.warning("[Feasibility] Skipped document attachment: %s", exc)




# =========================
# input assembly / plan configuration
# =========================
def _plan_item_config(workshop_id: int, task_type: str) -> Optional[Dict[str, Any]]:
    """Find plan item config JSON for a given task type."""
    try:
        aliases = {task_type, task_type.replace("-", "_"), task_type.replace("_", "-")}
        item = (
            WorkshopPlanItem.query
            .filter(
                WorkshopPlanItem.workshop_id == workshop_id,
                WorkshopPlanItem.enabled.is_(True),
                WorkshopPlanItem.task_type.in_(aliases),
            )
            .order_by(WorkshopPlanItem.order_index.asc())
            .first()
        )
        if not item or not item.config_json:
            return None
        if isinstance(item.config_json, str):
            return json.loads(item.config_json)
        if isinstance(item.config_json, dict):
            return item.config_json
    except Exception:
        current_app.logger.debug("[Feasibility] No plan config for %s", task_type, exc_info=True)
    return None


def _collect_overview(ws: Workshop) -> Dict[str, Any]:
    try:
        participant_count = ws.participants.count() if hasattr(ws.participants, "count") else len(list(ws.participants))  # type: ignore[arg-type]
    except Exception:
        participant_count = 0
    organizer = getattr(ws, "organizer", None)
    org_name = None
    if organizer:
        for attr in ("display_name", "first_name", "email"):
            org_name = getattr(organizer, attr, None)
            if org_name:
                break
    if not org_name:
        org_name = "Unknown organizer"
    return {
        "title": ws.title,
        "objective": ws.objective or "TBD",
        "scheduled_for": ws.date_time.strftime("%Y-%m-%d %H:%M UTC") if ws.date_time else "unscheduled",
        "duration_minutes": ws.duration,
        "status": ws.status,
        "organizer": org_name,
        "participant_count": participant_count,
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
        current_app.logger.debug("[Feasibility] Failed to load payload types=%s", types, exc_info=True)
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
    clusters: List[Dict[str, Any]] = []
    for r in rows:
        ideas = (
            BrainstormIdea.query.filter_by(cluster_id=r.cluster_id).order_by(BrainstormIdea.id.asc()).all()
        )
        clusters.append(
            {
                "cluster_id": int(r.cluster_id),
                "name": _safe(r.name),
                "description": _safe(r.description),
                "votes": int(r.votes or 0),
                "ideas": [
                    {
                        "idea_id": int(i.id),
                        "text": _safe(i.corrected_text or i.content),
                        "source": _safe(i.source),
                        "participant_id": int(i.participant_id),
                    }
                    for i in ideas
                ],
            }
        )
    return clusters


def _prepare_feasibility_inputs(workshop_id: int, previous_task_id: int, phase_context: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        raise FeasibilityGenerationError(f"Workshop {workshop_id} not found")

    overview = _collect_overview(ws)

    framing = _load_latest_payload(workshop_id, ["framing"]) or {}
    warmup = _load_latest_payload(workshop_id, ["warm-up", "warm_up", "introduction"]) or {}
    brainstorming = _load_latest_payload(workshop_id, ["brainstorming", "ideas"]) or {}
    clustering_voting = _load_latest_payload(workshop_id, ["clustering_voting"]) or {}

    # Rubrics / rules surfaced from framing (LLM must receive them)
    rubrics = {
        "tech_feasibility_rubric": framing.get("tech_feasibility_rubric"),
        "legal_compliance_rules": framing.get("legal_compliance_rules"),
        "budget_feasibility_rubric": framing.get("budget_feasibility_rubric"),
        "data_privacy_checklist": framing.get("data_privacy_checklist"),
        "ethical_considerations": framing.get("ethical_considerations"),
        "market_research_context": clustering_voting.get("market_research_context"),
        "market_target_segment": clustering_voting.get("market_target_segment") or "",
        "market_positioning": clustering_voting.get("market_positioning") or "",
        "go_to_market_strategy": clustering_voting.get("go_to_market_strategy") or "",
        "competitive_alternatives": clustering_voting.get("competitive_alternatives") or "",
    }

    # Clusters + ideas (corrected_text if present)
    clusters_full = _clusters_with_votes(previous_task_id)

    # Pre-workshop data
    try:
        prework_raw = get_pre_workshop_context_json(workshop_id)
        # Strip agenda durations to prevent LLM confusion with task duration
        prework_raw = strip_agenda_durations(prework_raw)
    except Exception:
        prework_raw = ""

    # Next phase snapshot (optional)
    next_phase = {"task_type": None, "phase": None, "duration": None, "description": None}
    try:
        # If you maintain a proper plan, you can add a real snapshot function here.
        pass
    except Exception:
        pass

    inputs = {
        "workshop_overview": json.dumps(overview, ensure_ascii=False, indent=2),
        "framing_json": json.dumps(
            {
                "problem_statement": framing.get("problem_statement"),
                "assumptions": framing.get("assumptions"),
                "constraints": framing.get("constraints"),
                "success_criteria": framing.get("success_criteria"),
                "context_summary": framing.get("context_summary"),
                "tech_feasibility_rubric": rubrics.get("tech_feasibility_rubric"),
                "legal_compliance_rules": rubrics.get("legal_compliance_rules"),
                "budget_feasibility_rubric": rubrics.get("budget_feasibility_rubric"),
                "data_privacy_checklist": rubrics.get("data_privacy_checklist"),
                "ethical_considerations": rubrics.get("ethical_considerations"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "warmup_json": json.dumps(
            {
                "title": warmup.get("title"),
                "instructions": warmup.get("instructions") or warmup.get("warm_up_instructions"),
                "participation_norms": warmup.get("participation_norms"),
                "selected_option": warmup.get("selected_option"),
                "energy_level": warmup.get("energy_level"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "brainstorming_json": json.dumps(brainstorming, ensure_ascii=False, indent=2),
        "clustering_voting_json": json.dumps(clustering_voting, ensure_ascii=False, indent=2),
        "clusters_full_json": json.dumps(clusters_full, ensure_ascii=False, indent=2),
        "pre_workshop_data": prework_raw,
        "current_phase_label": (phase_context or "Feasibility"),
        "phase_context": (phase_context or "Feasibility analysis of top clusters."),
        "feasibility_rules_and_rubrics": json.dumps(rubrics, ensure_ascii=False, indent=2),
        "next_phase_json": json.dumps(next_phase, ensure_ascii=False, indent=2),
    }
    meta = {"workshop_id": workshop_id}
    return inputs, meta



# =========================
# LLM Invocation
# =========================
def _invoke_feasibility_model(inputs: Dict[str, Any]) -> Dict[str, Any]:
    if not PromptTemplate:
        raise FeasibilityGenerationError("PromptTemplate unavailable")
    llm = get_chat_llm_pro(model_kwargs={
                                         "temperature": 0.35,
                                         "max_tokens": 4000,
                                         "top_k": 40,
                                         "top_p": 0.9
                                         })

    template = """
                You are the feasibility analyst and report author. Study the provided workshop data carefully.
                Analyze the top voted idea clusters for feasibility across multiple dimensions including technical, operational, legal/compliance, data privacy, financial, timeline, and risk.
                Identify key constraints, dependencies, regulatory notes, and ethical considerations.
                Using ONLY the data provided, and return *Only* ONE valid JSON object with the fields below.
                Do not omit or add extra keys, no markdown fences, no invalid escape sequences inside double quotes, no trailing commas, no whitespaces, no line breaks.
                
                
                Required top-level keys:
                - title: string value (use "Feasibility Analysis").
                - task_type: string value "results_feasibility".
                - task_description: one sentence purpose of the phase.
                - instructions: one short paragraph guidance with what and how to review the report.
                - task_duration: integer seconds.
                - narration: one paragraph in facilitator voice (objective/context, what was analyzed, how to read the report, how it feeds next steps).
                - tts_script: single paragraph that naturally like a human for text-to-speech, no lists or bullets, plain characters.
                - tts_read_time_seconds: integer ≥45 estimating read time for the tts_script.
                - analysis: object with the following structure:
                    * clusters: array of cluster analysis objects. For each cluster include:
                        - cluster_id (number)
                        - cluster_name (string)
                        - votes (number)
                        - feasibility_scores: object with keys technical, operational, legal_compliance, data_privacy, risk, cost_effort, time_to_value (each 1–5; higher risk/cost is worse; higher time_to_value means faster benefit).
                        - findings: object with arrays key_constraints, dependencies, regulatory_notes, data_privacy_notes, ethical_considerations, plus risks (array of objects with risk, severity low|medium|high, likelihood low|medium|high, mitigation string).
                        - recommendation: object providing summary text, an array next_steps[] of concrete follow-ups, and a confidence value (low|medium|high).
                        - representative_ideas: array of objects each containing idea_id (number) and text (string).
                    * method_notes: short paragraph on how rubrics/rules informed judgments.
                - document_spec: object with the following fields:
                        * title: string.
                        * cover: object containing subtitle, objective, date_str, and top_clusters (array with name and votes for each cluster).
                        * sections: array, ordered exactly as listed below. Each section supplies a heading string and blocks array:
                            1. Heading "Executive Summary" with two blocks: (a) type "p" summarizing viability using votes and constraints; (b) type "table" columns ["Dimension","Verdict"] with rows for Technical, Operational, Legal/Compliance, Data Privacy, Finance, Timeline, and Risk.
                            2. Heading "Top Clusters & Ideas" with blocks: narrative paragraph, then table columns ["Cluster","Votes","Representative Ideas"] derived strictly from clusters_full_json.
                            3. Heading "Technical Considerations" with blocks: paragraph on tech stack/integrations/R&D/staffing, and table columns ["Item","Detail"].
                            4. Heading "Market & Competitive Analysis" with blocks: paragraph using research context (write "TBD" when missing), and unordered list items Target segments, Positioning hypothesis, Competitive alternatives (or "TBD"), Go-to-market notes.
                            5. Heading "Operational Feasibility" with blocks: paragraph on org/process fit, and table columns ["Capability","Readiness"].
                            6. Heading "Legal & Compliance" with blocks: paragraph applying legal_compliance_rules, and unordered list items Key obligations, Gaps, Mitigations.
                            7. Heading "Data Privacy & Ethics" with blocks: paragraph referencing data_privacy_checklist and ethical_considerations, and unordered list items Data classes, Retention, Access controls, Ethical notes.
                            8. Heading "Financial Projection" with blocks: paragraph using budget_feasibility_rubric (state assumptions, use "TBD" for unknowns), and table columns ["Item","Estimate"] with rows One-time Cost, Annual Opex, Expected Benefit.
                            9. Heading "Project Timeline" with blocks: paragraph describing phases from discovery to rollout referencing time_to_value and constraints, and table columns ["Phase","Duration","Exit Criteria"].
                            10. Heading "Risk Register (FMEA-style)" with blocks: paragraph summarizing key risks, and table columns ["Risk","Severity","Likelihood","Mitigation"].
                            11. Heading "Recommendations & Decision" with blocks: paragraph providing cluster-level recommendations and overall go/hold/learn decision with rationale, plus unordered list of top next steps.
                        * appendices: optional array (for example "Scoring Details" per cluster) which may include tables.

                Hard rules:
                - Use ONLY provided inputs. If a detail is unknown, write "TBD".
                - DO NOT invent clusters or ideas; use cluster_id, names, votes from clusters_full_json.
                - Keep tables to <= 6 columns. 
                - Produce a valid JSON Object, no whitespaces, trailing commas, line breaks or empty arrays/objects. 

                Workshop Snapshot (JSON):
                {workshop_overview}

                Framing Highlights (JSON):
                {framing_json}

                Warm-Up Summary (JSON):
                {warmup_json}

                Brainstorming Summary (JSON):
                {brainstorming_json}

                Clustering & Voting (JSON):
                {clustering_voting_json}

                Clusters (ideas, votes) (JSON):
                {clusters_full_json}

                Pre-Workshop Research (may be truncated):
                {pre_workshop_data}

                Phase Label: {current_phase_label}

                Phase Context:
                {phase_context}

                Rubrics & Rules (JSON):
                {feasibility_rules_and_rubrics}

                Upcoming Phase (JSON):
                {next_phase_json}
                
                """
    prompt = PromptTemplate.from_template(template)
    chain = prompt | llm
    raw = chain.invoke(inputs)
    print("[Feasibility] LLM raw response:", raw)
    text = _coerce_text(raw)
    current_app.logger.debug(
        "[Feasibility] LLM raw response preview: %s",
        text[:1000] + ("…" if len(text) > 1000 else ""),
    )

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        block = extract_json_block(text)
        if block:
            try:
                data = json.loads(block)
            except Exception as exc:  # pragma: no cover - unexpected
                raise FeasibilityGenerationError(f"Model did not return valid JSON: {exc}") from exc
        else:
            # Fallback: fabricate placeholder payload that signals retry instead of failing hard.
            return {
                "title": "Feasibility Analysis",
                "task_type": "results_feasibility",
                "task_description": "Unable to complete feasibility generation due to malformed provider output.",
                "instructions": "Please retry in a few moments; the LLM response was incomplete.",
                "task_duration": 300,
                "narration": "We could not finalize the feasibility report because the model returned malformed JSON. Refresh shortly and try again.",
                "tts_script": "The feasibility analysis is delayed due to provider traffic. Please retry soon.",
                "tts_read_time_seconds": 60,
                "analysis": {"clusters": [], "method_notes": "Feasibility pending due to malformed response."},
                "document_spec": {
                    "title": "Feasibility Brief Pending",
                    "cover": {
                        "subtitle": "Retry soon",
                        "objective": "Awaiting valid feasibility report",
                        "date_str": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                        "top_clusters": [{"name": "TBD", "votes": 0}],
                    },
                    "sections": [
                        {
                            "heading": "Status",
                            "blocks": [
                                {
                                    "type": "p",
                                    "text": "The feasibility report is temporarily unavailable because the upstream LLM returned malformed JSON.",
                                },
                                {
                                    "type": "note",
                                    "text": "Retry the feasibility generation once provider traffic subsides.",
                                },
                            ],
                        }
                    ],
                    "appendices": [],
                },
            }
    except Exception as exc:  # pragma: no cover - unexpected
        raise FeasibilityGenerationError(f"Model did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FeasibilityGenerationError("Model output must be a JSON object.")
    return data



def generate_feasibility_text(
    workshop_id: int,
    clusters_summary: str,
    phase_context: str,
) -> Tuple[Dict[str, Any], int]:
    """Compatibility shim so tests can stub the LLM output."""
    try:
        inputs = json.loads(clusters_summary) if clusters_summary else {}
    except json.JSONDecodeError as exc:
        raise FeasibilityGenerationError("clusters_summary must be JSON") from exc

    if not isinstance(inputs, dict):
        raise FeasibilityGenerationError("clusters_summary JSON must decode to an object")

    # Ensure phase context survives round-trip even if caller overrides it.
    if phase_context:
        inputs.setdefault("phase_context", phase_context)
        inputs.setdefault("current_phase_label", phase_context)

    payload = _invoke_feasibility_model(inputs)
    return payload, 200



# =========================
# API Entry Point
# =========================
def get_feasibility_payload(workshop_id: int, previous_task_id: int, phase_context: str) -> Dict[str, Any] | Tuple[str, int]:
    """Generate feasibility results (single LLM call), persist task, render PDF, return payload."""
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return "Workshop not found", 404

    try:
        inputs, _meta = _prepare_feasibility_inputs(workshop_id, previous_task_id, phase_context)
    except FeasibilityGenerationError as exc:
        return str(exc), 400
    except Exception as exc:
        current_app.logger.error("[Feasibility] Input prep failed: %s", exc, exc_info=True)
        return "Failed to prepare feasibility inputs", 500

    try:
        clusters_summary = json.dumps(inputs, ensure_ascii=False)
        data_response = generate_feasibility_text(
            workshop_id,
            clusters_summary,
            phase_context or "",
        )
    except FeasibilityGenerationError as exc:
        payload = {
            "title": "Feasibility Analysis",
            "task_type": "results_feasibility",
            "task_description": "Feasibility report temporarily unavailable.",
            "instructions": "Please retry after a short pause; the assistant could not parse the provider response.",
            "task_duration": 300,
            "narration": "We couldn't render the feasibility summary because the upstream model response was malformed.",
            "tts_script": "Feasibility analysis is delayed because of a malformed provider response. Please try again shortly.",
            "tts_read_time_seconds": 60,
            "analysis": {"clusters": [], "method_notes": "Awaiting valid feasibility output."},
            "document_spec": {
                "title": "Feasibility Brief Pending",
                "cover": {
                    "subtitle": "Retry soon",
                    "objective": "Awaiting valid feasibility report",
                    "date_str": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                    "top_clusters": [{"name": "TBD", "votes": 0}],
                },
                "sections": [
                    {
                        "heading": "Status",
                        "blocks": [
                            {
                                "type": "p",
                                "text": "Feasibility analysis is temporarily unavailable due to a malformed provider response.",
                            },
                            {
                                "type": "note",
                                "text": "Retry once the provider response stabilizes.",
                            },
                        ],
                    }
                ],
                "appendices": [],
            },
        }
        current_app.logger.error("[Feasibility] LLM failure: %s", exc, exc_info=True)
        return payload, 200
    except Exception as exc:
        current_app.logger.error("[Feasibility] Unhandled LLM error: %s", exc, exc_info=True)
        return "Feasibility generation error", 503

    if isinstance(data_response, tuple):
        data, status_code = data_response
    else:
        data, status_code = data_response, 200

    if status_code >= 400:
        message = data if isinstance(data, str) else "Feasibility generation error"
        return message, status_code

    if not isinstance(data, dict):
        return "Feasibility output must be a JSON object", 500

    _ensure_feasibility_contract(data)

    # Strictly trust LLM fields (no fallback/rewrites)
    required = [
        "title",
        "task_type",
        "task_description",
        "instructions",
        "task_duration",
        "narration",
        "tts_script",
        "tts_read_time_seconds",
        "analysis",
        "document_spec",
    ]
    if not all(k in data for k in required):
        missing = [k for k in required if k not in data]
        current_app.logger.error(
            "[Feasibility] Output missing required fields %s. Payload preview: %s",
            missing,
            json.dumps({k: data.get(k) for k in required if k in data}, ensure_ascii=False)[:2000],
        )
        current_app.logger.debug(
            "[Feasibility] Full payload: %s",
            json.dumps(data, ensure_ascii=False)[:6000],
        )
        return "Feasibility output missing required fields", 500

    # Persist BrainstormTask with raw LLM payload only
    task = BrainstormTask()
    task.workshop_id = workshop_id
    task.task_type = _safe(data.get("task_type") or "results_feasibility")
    task.title = _safe(data.get("title") or "Feasibility Analysis")
    task.description = _safe(data.get("task_description"))
    task.duration = int(data.get("task_duration") or 600)
    task.status = "pending"
    payload_str = json.dumps(data, ensure_ascii=False)
    task.prompt = payload_str
    task.payload_json = payload_str
    db.session.add(task)
    db.session.flush()

    # Mirror task id into payload
    data["task_id"] = task.id

    # Render PDF from LLM-provided document_spec (no markdown, no fallback)
    try:
        assets = _generate_feasibility_pdf(ws, data.get("document_spec") or {})
        if assets:
            abs_path, rel_path, url = assets
            data["feasibility_pdf_path"] = rel_path
            data["feasibility_pdf_url"] = url
            data["pdf_document"] = url
            _attach_doc(ws, abs_path, rel_path, url, data)
            # Persist augmented payload with PDF fields (still LLM-authored content + file links)
            augmented = json.dumps(data, ensure_ascii=False)
            task.payload_json = augmented
            task.prompt = augmented
    except Exception as exc:
        # We still return the LLM payload if PDF fails, but we do NOT "re-author" any content.
        current_app.logger.error("[Feasibility] PDF generation error: %s", exc, exc_info=True)

    current_app.logger.info("[Feasibility] Created task %s for workshop %s", task.id, ws.id)
    return data
