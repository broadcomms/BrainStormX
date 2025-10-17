# app/service/routes/prioritization.py
"""
Prioritization & Shortlisting — LLM-only phase (No fallback heuristics)

Goal
- Transform feasibility and votes into a defensible shortlist.

What this module does
- Assembles full context from prior phases (framing, brainstorming, clustering/voting, feasibility)
- Calls the LLM once with a strict JSON contract (ICE/RICE + Kano + Impact–Effort placement)
- Persists the returned task and generates a Shortlist PDF from LLM-owned document_spec
- Returns the full payload for UI and downstream modules (discussion, action plan)

Expected LLM JSON (excerpt)
{
  "title": "Prioritization & Shortlisting",
  "task_type": "results_prioritization",
  "task_description": "...",
  "instructions": "...",
  "task_duration": 900,
  "narration": "...",
  "tts_script": "...",
  "tts_read_time_seconds": 75,
  "prioritized": [
    {
      "cluster_id": 12,
      "title": "AI Triage for Support",
      "description": "Implement AI-driven ticket triage for support.",
      "vote_count": 15,
      "rank": 1,
      "scores": {
        "RICE": 412,
        "ICE": 7.8,
        "Kano": 4.5,
        "impact": 9,
        "confidence": 8,
        "effort": 3,
        "feasibility": 9,
        "strategic_fit": 8,
        "success_criteria_alignment": 9
      },
      "weights": {"impact": 0.4, "confidence": 0.3, "effort": 0.3},
      "position": "HighImpact/LowEffort",
      "kano_type": "Performance",
      "why": "Strong feasibility, high impact, low effort, aligned with criteria.",
      "representative_ideas": [{"idea_id": 77, "text": "Auto-triage inbound tickets using AI"}],
      "theme_label": "AI triage for support",
      "theme_summary": "Automating routing to cut response time.",
      "duplicate_refs": [81, 104],
      "risks": [{"risk_id":"r1","risk":"Misclassification","severity":3,"likelihood":2,"mitigation":"HITL for 30 days"}]
    }
  ],
  "methods": ["RICE","ICE","Kano","Impact-Effort"],
  "risks": [...],
  "constraints": ["Budget capped at $75k", "Data must remainin-region"],
  "captured_decisions": [{"cluster_id":12,"topic":"Pilot scope","decision":"Proceed","user_id":7,"rationale":"..."}],
  "captured_action_items": [{"title":"Pilot AI triage","user_id":7,"metric":"Reduce FRT by 30%","cluster_id":12}],
  "open_unknowns": ["CRM integration complexity"],
  "notable_findings": ["42% of inbound tickets repetitive"],
  "impact_effort_chart": {
    "type": "scatter",
    "data": {
      "x": [3],
      "y": [9],
      "labels": ["AI Triage for Support"]
    },
    "layout": {
      "title": "Impact–Effort Chart",
      "xaxis": {"title": "Effort"},
      "yaxis": {"title": "Impact"}
    }
  },
  "document_spec": {
    "title": "Workshop Shortlist — Prioritized Ideas",
    "cover": {
      "subtitle": "From feasibility and voting to a defensible shortlist",
      "date_str": "2025-10-02 10:00 UTC",
      "objective": "Select top ideas for execution planning.",
      "topline": {"clusters_considered": 5, "ideas_considered": 23},
      "weights_table": {"columns":["Factor","Weight"],"rows":[["Impact","0.4"],["Confidence","0.3"],["Effort","0.3"]]}
    },
    "sections": [
      {"heading":"Executive Summary","blocks":[{"type":"p","text":"..."}]},
      {"heading":"Shortlist Table","blocks":[{"type":"table","columns":["Rank","Cluster","Votes","RICE","ICE","Impact","Effort","Feasibility","Fit"],"rows":[["1","AI Triage for Support","15","412","7.8","9","3","9","8"]]}]},
      {"heading":"Top Candidates — Details","blocks":[{"type":"h2","text":"AI Triage for Support"},{"type":"p","text":"..."},{"type":"ul","items":["Why now: ...","Risks: ...","Next steps: ..."]}]},
      {"heading":"Impact–Effort Placement","blocks":[{"type":"note","text":"Auto-placed by LLM: High impact / Low effort"}]},
      {"heading":"Recommendations","blocks":[{"type":"p","text":"..." }]}
    ]
  }
}
"""
from __future__ import annotations

import os
import json
from datetime import datetime
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

VOTE_WEIGHT = 0.5
FEASIBILITY_WEIGHT = 0.3
EFFORT_WEIGHT = 0.2

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.config import Config
from app.models import (
    Workshop,
    BrainstormTask,
    IdeaCluster,
    IdeaVote,
    BrainstormIdea,
    WorkshopPlanItem,
    Document,
    WorkshopDocument,
)
from app.utils.agenda_utils import strip_agenda_durations
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.json_utils import extract_json_block
from app.utils.llm_bedrock import get_chat_llm_pro
from langchain_core.prompts import PromptTemplate
from app.service.routes.presentation import _build_shortlist as _presentation_build_shortlist



# ---------- small utils ----------
def _safe(x: Any) -> str:
    return (str(x) if x is not None else "").strip()

def _reports_dir() -> str:
    base = os.path.join(current_app.instance_path, "uploads", "reports")
    os.makedirs(base, exist_ok=True)
    return base

def _safe_title(s: str) -> str:
    return (s or "Workshop").replace("/", "-").strip()


# ---------- shortlist helpers ----------
def _mean_or_default(values: List[Any], default: float = 0.0) -> float:
    items = [float(v) for v in values if isinstance(v, (int, float))]
    return float(mean(items)) if items else float(default)


def _normalize(value: Optional[float], *, minimum: float = 0.0, maximum: float = 5.0, default: float = 0.5) -> float:
    if not isinstance(value, (int, float)):
        return float(default)
    span = maximum - minimum
    if span <= 0:
        return float(default)
    normalized = (float(value) - minimum) / span
    return min(max(normalized, 0.0), 1.0)


def _invert(value: float) -> float:
    return 1.0 - min(max(value, 0.0), 1.0)


def _classify_position(impact_score: float, effort_advantage: float) -> str:
    if impact_score >= 0.7 and effort_advantage >= 0.6:
        return "High Impact/Low Effort"
    if impact_score >= 0.7 and effort_advantage < 0.6:
        return "High Impact/High Effort"
    if impact_score >= 0.4 and effort_advantage >= 0.6:
        return "Low Impact/Low Effort"
    return "Low Impact/High Effort"
def _build_shortlist(
    workshop_id: int,
    weights: Optional[Dict[str, float]] = None,
    constraints: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Expose presentation shortlist heuristic for compatibility/tests."""
    default_weights = weights or {"votes": 1.0, "feasibility": 1.0, "objective_fit": 1.0}
    default_constraints = constraints or {}
    return _presentation_build_shortlist(workshop_id, default_weights, default_constraints)

# ---------- PDF (ReportLab) JSON renderer ----------
def _render_shortlist_pdf(workshop: Workshop, spec: Dict[str, Any]) -> Tuple[str, str, str] | None:
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
        current_app.logger.warning("[Prioritization] ReportLab unavailable: %s", exc)
        return None

    ts = datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S")
    fname = f"{_safe_title(workshop.title)} shortlist {ts}.pdf"
    abs_path = os.path.join(_reports_dir(), fname)
    rel_path = os.path.join("uploads", "reports", fname)

    base = getSampleStyleSheet()
    Title = ParagraphStyle("BX_Title", parent=base["Title"], fontSize=24, leading=28,
                           alignment=TA_LEFT, spaceBefore=18, spaceAfter=10,
                           textColor=colors.HexColor("#111827"))
    H1 = ParagraphStyle("BX_H1", parent=base["Heading2"], fontSize=16, leading=20,
                        spaceBefore=16, spaceAfter=8, textColor=colors.HexColor("#111827"))
    H2 = ParagraphStyle("BX_H2", parent=base["Heading3"], fontSize=13, leading=17,
                        spaceBefore=10, spaceAfter=6, textColor=colors.HexColor("#111827"))
    Body = ParagraphStyle("BX_Body", parent=base["BodyText"], fontSize=10.5, leading=14,
                          textColor=colors.HexColor("#111827"), spaceAfter=6)
    Note = ParagraphStyle("BX_Note", parent=base["BodyText"], fontSize=9.5, leading=13,
                          textColor=colors.HexColor("#6B7280"), spaceAfter=6)

    def rule(space=8, color="#E5E7EB"):
        t = Table([[""]], colWidths=[6.5 * inch], rowHeights=[0.7])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(color))]))
        return [Spacer(1, space), t, Spacer(1, space)]

    def _table(cols: List[str], rows: List[List[str]]) -> Table:
        data = [[Paragraph(_safe(c), Body) for c in cols]]
        for r in rows or []:
            data.append([Paragraph(_safe(c), Body) for c in r])
        tbl = Table(data, colWidths=[(6.5 * inch) / max(1, len(cols))] * max(1, len(cols)))
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#F3F4F6")),
            ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#D1D5DB")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        return tbl

    def _ul(items: List[str]) -> ListFlowable:
        li = [ListItem(Paragraph(_safe(i), Body)) for i in items or [] if _safe(i)]
        return ListFlowable(li, bulletType="bullet", leftIndent=12)

    def _footer(canv: canvas.Canvas, doc):
        canv.setFont("Helvetica", 9)
        canv.setFillColor(colors.HexColor("#6B7280"))
        canv.drawRightString(7.95 * inch, 0.5 * inch, f"Page {doc.page}")

    frame = Frame(0.75 * inch, 0.75 * inch, 7.0 * inch, 9.75 * inch, showBoundary=0)
    doc = BaseDocTemplate(abs_path, pagesize=LETTER, leftMargin=0, rightMargin=0, topMargin=0, bottomMargin=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_footer)])

    E: List[Any] = []
    E.append(Paragraph(_safe(spec.get("title") or f"{workshop.title} — Shortlist"), Title))
    E += rule(color="#0d6efd")

    cover = spec.get("cover") or {}
    subtitle = _safe(cover.get("subtitle"))
    if subtitle:
        E.append(Paragraph(subtitle, Note))
    obj = _safe(cover.get("objective") or getattr(workshop, "objective", ""))
    if obj:
        E.append(Paragraph("Objective", H2))
        E.append(Paragraph(obj, Body))
    date_str = _safe(cover.get("date_str") or (workshop.date_time.strftime("%Y-%m-%d %H:%M UTC") if getattr(workshop, "date_time", None) else "TBD"))
    E.append(Paragraph("Date", H2))
    E.append(Paragraph(date_str, Body))
    top_line = cover.get("topline") or {}
    if top_line:
        cols = ["Metric", "Value"]
        rows = [[k.replace("_"," ").title(), str(v)] for k, v in top_line.items()]
        E.append(_table(cols, rows))
    wt = (cover.get("weights_table") or {})
    if wt:
        E.append(Paragraph("Weights:", H2))
        E.append(_table(wt.get("columns") or [], wt.get("rows") or []))
    E.append(PageBreak())

    seen_rationale = False
    seen_legend = False

    for sec in spec.get("sections", []):
        heading = _safe(sec.get("heading"))
        if heading:
            E.append(Paragraph(heading, H1))
            if "rationale" in heading.lower():
                seen_rationale = True
            if "legend" in heading.lower():
                seen_legend = True
        for blk in sec.get("blocks", []):
            typ = (blk.get("type") or "p").lower()
            if typ == "p":
                E.append(Paragraph(_safe(blk.get("text")), Body))
            elif typ == "h2":
                E.append(Paragraph(_safe(blk.get("text")), H2))
            elif typ == "ul":
                E.append(_ul([_safe(x) for x in blk.get("items", [])]))
            elif typ == "table":
                E.append(_table(blk.get("columns") or [], blk.get("rows") or []))
            elif typ == "note":
                E.append(Paragraph(_safe(blk.get("text")), Note))
            elif typ == "rule":
                E += rule()
            elif typ == "page_break":
                E.append(PageBreak())

    E += rule()

    if not seen_rationale:
        E.append(Paragraph("Rationale", H1))
        rationale_note = _safe(spec.get("rationale_note") or "Weights and scoring drivers for the shortlist.")
        E.append(Paragraph(rationale_note, Body))
        weights_table = (spec.get("cover") or {}).get("weights_table") or {}
        columns = weights_table.get("columns") or ["Factor", "Weight"]
        rows = weights_table.get("rows") or [["Impact", ""], ["Confidence", ""], ["Effort", ""]]
        E.append(_table(columns, rows))

    if not seen_legend:
        E.append(Paragraph("Legend", H1))
        legend_items = [
            "High Impact / Low Effort — quick wins",
            "High Impact / High Effort — strategic bets",
            "Lower Impact ideas require re-evaluation",
        ]
        E.append(_ul(legend_items))

    E.append(Paragraph(f"Prepared by BrainStormX • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", Note))

    try:
        doc.build(E)
    except Exception as exc:
        current_app.logger.error("[Prioritization] Shortlist PDF build failed: %s", exc, exc_info=True)
        return None

    url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{os.path.basename(rel_path)}"
    return abs_path, rel_path, url

# ---------- document attachment ----------
def _attach_doc(ws: Workshop, abs_path: str, rel_path: str, url: str, payload: Dict[str, Any]) -> None:
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        size = None
    try:
        doc = Document()
        doc.workspace_id = ws.workspace_id
        doc.title = f"{ws.title} — Shortlist Report"
        doc.description = "LLM-generated prioritization shortlist report"
        doc.file_name = os.path.basename(rel_path)
        doc.file_path = rel_path
        doc.file_size = size
        doc.uploaded_by_id = ws.created_by_id
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
        payload["shortlist_document"] = dict(doc_payload)
        payload["document"] = dict(doc_payload)
    except Exception as exc:
        current_app.logger.warning("[Prioritization] Skipped document attachment: %s", exc)

# ---------- input assembly ----------
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
        current_app.logger.debug("[Prioritization] Could not load payload types=%s", types, exc_info=True)
        return None

def _clusters_full(previous_task_id: Optional[int]) -> List[Dict[str, Any]]:
    if not previous_task_id:
        return []
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
    result: List[Dict[str, Any]] = []
    for r in rows:
        ideas = BrainstormIdea.query.filter_by(cluster_id=r.cluster_id).order_by(BrainstormIdea.id.asc()).all()
        result.append({
            "cluster_id": int(r.cluster_id),
            "name": _safe(r.name),
            "description": _safe(r.description),
            "votes": int(r.votes or 0),
            "ideas": [{"idea_id": int(i.id), "text": _safe(i.corrected_text or i.content)} for i in ideas],
        })
    return result

def _plan_item_config(workshop_id: int, task_type: str) -> Optional[Dict[str, Any]]:
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
        if isinstance(item.config_json, dict):
            return item.config_json
        if isinstance(item.config_json, str):
            return json.loads(item.config_json)
    except Exception:
        current_app.logger.debug("[Prioritization] No plan config for %s", task_type, exc_info=True)
    return None


def _index_feasibility(feasibility_payload: Optional[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    mapping: Dict[int, Dict[str, Any]] = {}
    if not isinstance(feasibility_payload, dict):
        return mapping
    analysis = feasibility_payload.get("analysis")
    clusters = analysis.get("clusters") if isinstance(analysis, dict) else None
    if not isinstance(clusters, list):
        return mapping
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        try:
            cluster_id = int(cluster.get("cluster_id"))
        except (TypeError, ValueError):
            continue
        mapping[cluster_id] = cluster
    return mapping


def _compute_cluster_metrics(
    clusters_full: List[Dict[str, Any]],
    feasibility_payload: Optional[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    metrics: Dict[int, Dict[str, Any]] = {}
    max_votes = max((c.get("votes", 0) for c in clusters_full), default=0) or 1
    feasibility_index = _index_feasibility(feasibility_payload)

    for cluster in clusters_full:
        cluster_id = int(cluster["cluster_id"])
        votes = int(cluster.get("votes", 0))
        vote_norm = votes / max_votes if max_votes else 0.0

        feasibility = feasibility_index.get(cluster_id, {})
        scores = feasibility.get("feasibility_scores") if isinstance(feasibility.get("feasibility_scores"), dict) else {}

        feasibility_norm = _mean_or_default([
            _normalize(scores.get(key))
            for key in ("technical", "operational", "legal_compliance", "data_privacy", "risk", "time_to_value")
        ])
        confidence_norm = _mean_or_default([
            _normalize(scores.get(key))
            for key in ("technical", "operational", "legal_compliance", "data_privacy")
        ])
        effort_advantage = _invert(_normalize(scores.get("cost_effort")))

        baseline_score = (
            VOTE_WEIGHT * vote_norm
            + FEASIBILITY_WEIGHT * feasibility_norm
            + EFFORT_WEIGHT * effort_advantage
        )

        findings = feasibility.get("findings") if isinstance(feasibility.get("findings"), dict) else {}
        recommendation = feasibility.get("recommendation") if isinstance(feasibility.get("recommendation"), dict) else {}

        metrics[cluster_id] = {
            "votes": votes,
            "vote_norm": vote_norm,
            "feasibility_norm": feasibility_norm,
            "confidence_norm": confidence_norm,
            "effort_advantage": effort_advantage,
            "baseline_score": baseline_score,
            "findings": findings,
            "recommendation": recommendation,
            "risks": findings.get("risks") if isinstance(findings.get("risks"), list) else [],
            "constraints": findings.get("key_constraints") if isinstance(findings.get("key_constraints"), list) else [],
            "dependencies": findings.get("dependencies") if isinstance(findings.get("dependencies"), list) else [],
            "representative_ideas": feasibility.get("representative_ideas") if isinstance(feasibility.get("representative_ideas"), list) else cluster.get("ideas", []),
        }
    return metrics


def _build_deterministic_prioritized(
    clusters_full: List[Dict[str, Any]],
    metrics: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    prioritized: List[Dict[str, Any]] = []
    sorted_clusters = sorted(
        clusters_full,
        key=lambda c: metrics.get(int(c["cluster_id"]), {}).get("baseline_score", 0.0),
        reverse=True,
    )
    for idx, cluster in enumerate(sorted_clusters, start=1):
        cluster_id = int(cluster["cluster_id"])
        metric = metrics.get(cluster_id, {})
        impact_norm = metric.get("vote_norm", 0.0)
        feasibility_norm = metric.get("feasibility_norm", 0.5)
        confidence_norm = metric.get("confidence_norm", 0.5)
        effort_advantage = metric.get("effort_advantage", 0.5)
        baseline_score = metric.get("baseline_score", 0.0)

        impact_pct = round(impact_norm * 100, 1)
        feasibility_pct = round(feasibility_norm * 100, 1)
        confidence_pct = round(confidence_norm * 100, 1)
        effort_pct = round(effort_advantage * 100, 1)

        position = _classify_position(impact_norm, effort_advantage)
        why = ", ".join(
            [
                f"Vote share {metric.get('votes', 0)} ({impact_pct:.0f}/100 impact)",
                f"Feasibility {feasibility_pct:.0f}/100",
                f"Effort advantage {effort_pct:.0f}/100",
            ]
        )

        representative = metric.get("representative_ideas") or cluster.get("ideas", [])

        prioritized.append(
            {
                "cluster_id": cluster_id,
                "title": cluster.get("name"),
                "description": cluster.get("description"),
                "vote_count": metric.get("votes", 0),
                "rank": idx,
                "scores": {
                    "impact": round(impact_pct / 10, 1),
                    "confidence": round(confidence_pct / 10, 1),
                    "effort": round((100 - effort_pct) / 10, 1),
                    "feasibility": round(feasibility_pct / 10, 1),
                    "baseline_score": round(baseline_score, 3),
                },
                "weights": {
                    "votes": VOTE_WEIGHT,
                    "feasibility": FEASIBILITY_WEIGHT,
                    "effort": EFFORT_WEIGHT,
                },
                "position": position,
                "why": why,
                "representative_ideas": representative[:2],
                "theme_label": cluster.get("name"),
                "theme_summary": cluster.get("description"),
                "risks": metric.get("risks", []),
            }
        )
    return prioritized


def _summarise_constraints(metrics: Dict[int, Dict[str, Any]]) -> List[str]:
    constraints: List[str] = []
    for metric in metrics.values():
        for constraint in metric.get("constraints", []) or []:
            text = _safe(constraint)
            if text and text not in constraints:
                constraints.append(text)
    return constraints[:10]


def _summarise_risks(prioritized: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    risks: List[Dict[str, Any]] = []
    for item in prioritized:
        for risk in item.get("risks", []):
            if not isinstance(risk, dict):
                continue
            label = risk.get("risk") or risk.get("description") or _safe(risk)
            if label:
                risks.append(
                    {
                        "cluster_id": item.get("cluster_id"),
                        "risk": label,
                        "severity": risk.get("severity"),
                        "likelihood": risk.get("likelihood"),
                        "mitigation": risk.get("mitigation"),
                    }
                )
    return risks[:12]


def _summarise_open_unknowns(metrics: Dict[int, Dict[str, Any]]) -> List[str]:
    unknowns: List[str] = []
    for metric in metrics.values():
        for dependency in metric.get("dependencies", []) or []:
            text = _safe(dependency)
            if text and text not in unknowns:
                unknowns.append(text)
    return unknowns[:10]


def _summarise_decisions(prioritized: List[Dict[str, Any]], metrics: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    decisions: List[Dict[str, Any]] = []
    for item in prioritized[:3]:
        cluster_id = item.get("cluster_id")
        recommendation = metrics.get(cluster_id, {}).get("recommendation", {})
        summary = recommendation.get("summary") if isinstance(recommendation, dict) else None
        if summary:
            decisions.append(
                {
                    "cluster_id": cluster_id,
                    "topic": item.get("title"),
                    "decision": summary,
                    "rationale": item.get("why"),
                }
            )
    return decisions


def _summarise_action_items(metrics: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for cluster_id, metric in metrics.items():
        recommendation = metric.get("recommendation")
        if not isinstance(recommendation, dict):
            continue
        steps = recommendation.get("next_steps")
        if not isinstance(steps, list):
            continue
        for step in steps[:3]:
            text = _safe(step)
            if text:
                items.append({"title": text, "cluster_id": cluster_id})
    return items[:12]


def _summarise_findings(prioritized: List[Dict[str, Any]]) -> List[str]:
    findings: List[str] = []
    for item in prioritized:
        why = _safe(item.get("why"))
        if why:
            findings.append(why)
    return findings[:10]


def _exec_summary(prioritized: List[Dict[str, Any]]) -> str:
    if not prioritized:
        return "No clusters were available for prioritization."
    top_names = [p.get("title") for p in prioritized[:3] if p.get("title")]
    names_text = ", ".join(top_names)
    leader = prioritized[0]
    return (
        f"Top priorities are {names_text}. {leader.get('title')} leads based on vote share and feasibility, "
        "followed closely by the remaining shortlisted clusters."
    )


def _recommendations_text(prioritized: List[Dict[str, Any]], metrics: Dict[int, Dict[str, Any]]) -> str:
    if not prioritized:
        return "Capture additional inputs before prioritizing recommendations."
    notes: List[str] = []
    for item in prioritized[:3]:
        cluster_id = item.get("cluster_id")
        recommendation = metrics.get(cluster_id, {}).get("recommendation")
        if isinstance(recommendation, dict) and recommendation.get("summary"):
            notes.append(f"{item.get('title')}: {recommendation['summary']}")
    if not notes:
        notes.append("Use the shortlist to stage pilots for the highest-ranked concepts.")
    return "\n".join(notes)


def _build_document_spec(
    workshop: Workshop,
    prioritized: List[Dict[str, Any]],
    metrics: Dict[int, Dict[str, Any]],
    exec_summary: str,
    recommendations: str,
) -> Dict[str, Any]:
    clusters_considered = len(prioritized)
    ideas_considered = sum(len(metrics.get(p.get("cluster_id"), {}).get("representative_ideas") or []) for p in prioritized)

    table_rows = []
    for item in prioritized:
        scores = item.get("scores", {})
        rice = int(round(scores.get("baseline_score", 0) * 100))
        ice = int(round((scores.get("impact", 0) + scores.get("confidence", 0) + max(scores.get("effort", 0), 0)) / 3 * 10))
        table_rows.append([
            item.get("rank"),
            item.get("title"),
            item.get("vote_count"),
            rice,
            ice,
            scores.get("impact"),
            scores.get("effort"),
            scores.get("feasibility"),
            scores.get("confidence"),
        ])

    details_blocks: List[Dict[str, Any]] = []
    for item in prioritized:
        details_blocks.append({"type": "h2", "text": item.get("title")})
        details_blocks.append({"type": "p", "text": item.get("why")})
        ideas = item.get("representative_ideas") or []
        bullets = [idea.get("text") for idea in ideas if idea.get("text")]
        if bullets:
            details_blocks.append({"type": "ul", "items": bullets})

    impact_notes = [f"{item.get('title')}: {item.get('position')}" for item in prioritized[:5]]

    weights_table = {
        "columns": ["Factor", "Weight"],
        "rows": [
            ["Votes", VOTE_WEIGHT],
            ["Feasibility", FEASIBILITY_WEIGHT],
            ["Effort Advantage", EFFORT_WEIGHT],
        ],
    }

    return {
        "title": "Prioritization & Shortlisting Report",
        "cover": {
            "subtitle": "Selecting the Most Promising Sustainable Mobility Concepts",
            "objective": _safe(workshop.objective) or "Prioritize ideas for next steps",
            "date_str": datetime.utcnow().strftime("%Y-%m-%d"),
            "topline": {
                "clusters_considered": clusters_considered,
                "ideas_considered": ideas_considered,
            },
            "weights_table": weights_table,
        },
        "sections": [
            {
                "heading": "Executive Summary",
                "blocks": [{"type": "p", "text": exec_summary}],
            },
            {
                "heading": "Shortlist Table",
                "blocks": [
                    {
                        "type": "table",
                        "columns": ["Rank", "Cluster", "Votes", "RICE", "ICE", "Impact", "Effort", "Feasibility", "Fit"],
                        "rows": table_rows,
                    }
                ],
            },
            {
                "heading": "Top Candidates — Details",
                "blocks": details_blocks,
            },
            {
                "heading": "Impact–Effort Placement",
                "blocks": [{"type": "note", "text": "\n".join(impact_notes)}],
            },
            {
                "heading": "Recommendations",
                "blocks": [{"type": "p", "text": recommendations}],
            },
        ],
    }


def _apply_deterministic_overlay(
    data: Dict[str, Any],
    workshop: Workshop,
    clusters_full: List[Dict[str, Any]],
    feasibility_payload: Optional[Dict[str, Any]],
) -> None:
    metrics = _compute_cluster_metrics(clusters_full, feasibility_payload)
    prioritized = _build_deterministic_prioritized(clusters_full, metrics)
    if prioritized:
        data["prioritized"] = prioritized
    methods = data.get("methods") if isinstance(data.get("methods"), list) else []
    composite_label = "Composite scoring: votes 50%, feasibility signals 30%, effort advantage 20%"
    if composite_label not in methods:
        methods = [composite_label] + methods
    data["methods"] = methods
    data["constraints"] = _summarise_constraints(metrics)
    data["risks"] = _summarise_risks(prioritized)
    data["captured_decisions"] = _summarise_decisions(prioritized, metrics)
    data["captured_action_items"] = _summarise_action_items(metrics)
    data["open_unknowns"] = _summarise_open_unknowns(metrics)
    data["notable_findings"] = _summarise_findings(prioritized)

    exec_summary = _exec_summary(prioritized)
    recommendations = _recommendations_text(prioritized, metrics)
    data["document_spec"] = _build_document_spec(workshop, prioritized, metrics, exec_summary, recommendations)
    data["narration"] = "We've combined vote strength, feasibility analysis, and effort expectations to build a grounded shortlist. " + exec_summary
    data["tts_script"] = (
        "Let's review the shortlist. "
        f"{exec_summary} We will discuss these clusters and confirm the next steps from the recommendations section."
    )

def _prepare_prioritization_inputs(
    workshop_id: int,
    previous_task_id: Optional[int],
    phase_context: Optional[str],
) -> Dict[str, Any]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        raise ValueError("Workshop not found")

    overview = {
        "title": ws.title,
        "objective": ws.objective or "TBD",
        "scheduled_for": ws.date_time.strftime("%Y-%m-%d %H:%M UTC") if ws.date_time else "unscheduled",
        "duration_minutes": ws.duration,
        "status": ws.status,
    }
    framing = _load_latest_payload(workshop_id, ["framing"]) or {}
    brainstorming = _load_latest_payload(workshop_id, ["brainstorming", "ideas"]) or {}
    clustering_voting = _load_latest_payload(workshop_id, ["clustering_voting"]) or {}
    feasibility = _load_latest_payload(workshop_id, ["results_feasibility"]) or {}

    clusters_full = _clusters_full(previous_task_id)
    shortlist, rationale = _build_shortlist(
        workshop_id,
        {"votes": 1.0, "feasibility": 1.0, "objective_fit": 1.0},
        {},
    )
    vote_summary: List[Dict[str, Any]] = []
    max_votes = max((c.get("votes", 0) for c in clusters_full), default=0) or 1
    for cluster in clusters_full:
        votes = int(cluster.get("votes", 0))
        vote_summary.append(
            {
                "cluster_id": cluster.get("cluster_id"),
                "name": cluster.get("name"),
                "votes": votes,
                "normalized_votes": round(votes / max_votes, 4) if max_votes else 0.0,
            }
        )

    feasibility_scores_summary: List[Dict[str, Any]] = []
    feasibility_clusters = feasibility.get("analysis", {}).get("clusters") if isinstance(feasibility.get("analysis"), dict) else None
    if isinstance(feasibility_clusters, list):
        for cluster in feasibility_clusters:
            if not isinstance(cluster, dict):
                continue
            scores = cluster.get("feasibility_scores") if isinstance(cluster.get("feasibility_scores"), dict) else {}
            feasibility_scores_summary.append(
                {
                    "cluster_id": cluster.get("cluster_id"),
                    "cluster_name": cluster.get("cluster_name") or cluster.get("name"),
                    "scores": scores,
                    "recommendation": cluster.get("recommendation"),
                }
            )

    prework = get_pre_workshop_context_json(workshop_id)
    # Strip agenda durations to prevent LLM confusion with task duration
    prework = strip_agenda_durations(prework)

    rubrics = {
        "tech_feasibility_rubric": framing.get("tech_feasibility_rubric"),
        "legal_compliance_rules": framing.get("legal_compliance_rules"),
        "budget_feasibility_rubric": framing.get("budget_feasibility_rubric"),
        "data_privacy_checklist": framing.get("data_privacy_checklist"),
        "success_criteria": framing.get("success_criteria"),
    }

    next_phase = _plan_item_config(workshop_id, "discussion") or {"task_type": "discussion"}

    return {
        "workshop_overview": json.dumps(overview, ensure_ascii=False, indent=2),
        "framing_json": json.dumps({
            "problem_statement": framing.get("problem_statement"),
            "assumptions": framing.get("assumptions"),
            "constraints": framing.get("constraints"),
            "success_criteria": framing.get("success_criteria"),
            "context_summary": framing.get("context_summary"),
        }, ensure_ascii=False, indent=2),
        "brainstorming_json": json.dumps(brainstorming, ensure_ascii=False, indent=2),
        "current_phase_label": (phase_context or "Prioritization"),
        "phase_context": (phase_context or "Prioritization phase to shortlist ideas based on feasibility and votes."),
        "clustering_voting_json": json.dumps(clustering_voting, ensure_ascii=False, indent=2),
        "clusters_full_json": json.dumps(clusters_full, ensure_ascii=False, indent=2),
        "shortlist_baseline_json": json.dumps(shortlist, ensure_ascii=False, indent=2),
        "shortlist_rationale_json": json.dumps(rationale, ensure_ascii=False, indent=2),
        "vote_summary_json": json.dumps(vote_summary, ensure_ascii=False, indent=2),
        "feasibility_scores_json": json.dumps(feasibility_scores_summary, ensure_ascii=False, indent=2),
        "feasibility_json": json.dumps({
            "title": feasibility.get("title"),
            "analysis": feasibility.get("analysis"),
        }, ensure_ascii=False, indent=2),
        "feasibility_analysis": json.dumps(feasibility.get("analysis"), ensure_ascii=False, indent=2),
        "feasibility_report": json.dumps(feasibility.get("document_spec"), ensure_ascii=False, indent=2),
        "feasibility_rules_and_rubrics": json.dumps(rubrics, ensure_ascii=False, indent=2),
        "pre_workshop_data": prework,
        "next_phase_json": json.dumps(next_phase, ensure_ascii=False, indent=2),
    }

# ---------- LLM call ----------
def _invoke_prioritization_model(inputs: Dict[str, Any]) -> Dict[str, Any]:
    prompt_template = """
You are a pragmatic product strategist. Use ONLY the provided data to produce a single STRICT JSON object for the
Prioritization & Shortlist phase of the workshop. Begin from the provided shortlist baseline and quantitative metrics.
If you adjust ranking or scores, explicitly ground the change in the vote summary or feasibility scores. Do not invent
new ideas or numeric values.

Contract (required top-level keys):
- title: string, use "Prioritization & Shortlisting".
- task_type: "results_prioritization".
- task_description: one sentence.
- instructions: one paragraph for participants.
- task_duration: integer seconds.
- narration: one paragraph in facilitator voice (what we did, what to review, how we'll use it).
- tts_script: one paragraph for TTS (natural speech, no lists).
- tts_read_time_seconds: integer >= 45.
- prioritized: array of cluster entries (honour the baseline order unless data justifies a swap). For each entry include:
    * cluster_id (number), title (string), description (string), vote_count (number), rank (number),
    * scores: object with RICE, ICE, Kano, impact, confidence, effort, feasibility, strategic_fit, success_criteria_alignment,
    * weights: object describing the weighting used,
    * position: "High Impact/Low Effort" | "High Impact/High Effort" | "Low Impact/Low Effort" | "Low Impact/High Effort",
    * kano_type: "Basic" | "Performance" | "Excitement" | "Indifferent" | "Reverse",
    * why: short rationale citing votes/feasibility/effort metrics,
    * representative_ideas: array of {idea_id:number, text:string},
    * theme_label, theme_summary, duplicate_refs (array of idea ids),
    * risks: array of {risk_id?:string, risk:string, severity:1-5, likelihood:1-5, mitigation:string}.
- methods: array describing which metrics were used (e.g., baseline composite with vote/feasibility/effort signals).
- risks: array summarizing cross-cutting risks (same schema as above but cluster_id optional).
- constraints: array of strings derived from framing or feasibility.
- captured_decisions: array of {cluster_id:number, topic:string, decision:string, user_id?:number, rationale:string}.
- captured_action_items: array of {title:string, user_id?:number, metric?:string, cluster_id?:number}.
- open_unknowns: array of strings.
- notable_findings: array of strings.
- document_spec: object for PDF rendering with:
    * title, cover {subtitle, objective, date_str, topline {clusters_considered, ideas_considered},
      weights_table {columns:["Factor","Weight"], rows:[[factor, weight], ...]}},
    * sections: [
        {"heading":"Executive Summary","blocks":[{"type":"p","text":"..."}]},
        {"heading":"Shortlist Table","blocks":[{"type":"table","columns":["Rank","Cluster","Votes","RICE","ICE","Impact","Effort","Feasibility","Fit"],"rows":[...]}]},
        {"heading":"Top Candidates — Details","blocks":[ ... paragraphs, bullets, subheads ... ]},
        {"heading":"Impact–Effort Placement","blocks":[{"type":"note","text":"Auto-placed quadrant notes"}]},
        {"heading":"Recommendations","blocks":[{"type":"p","text":"..."}]}
      ]
    }

Hard rules:
- Do NOT invent clusters or ideas; only use names/ids from clusters_full_json.
- Base numeric outputs on the provided vote_summary_json and feasibility_scores_json.
- If a detail is unknown, write "TBD".
- Clean valid JSON only; no markdown, no code fences, no trailing commas, no unnecessary whitespace, no line breaks outside JSON.
- Use double quotes for strings, no single quotes.

Workshop Snapshot (JSON):
{{ workshop_overview }}

Framing Highlights (JSON):
{{ framing_json }}

Brainstorming Summary (JSON):
{{ brainstorming_json }}

Current Phase Label: {{ current_phase_label }}

Phase Context Narrative:
{{ phase_context }}

Clustering & Voting (JSON):
{{ clustering_voting_json }}

Clusters (ideas, votes) (JSON):
{{ clusters_full_json }}

Vote Summary (JSON):
{{ vote_summary_json }}

Shortlist Baseline (JSON):
{{ shortlist_baseline_json }}

Shortlist Rationale (JSON):
{{ shortlist_rationale_json }}

Feasibility Summary (JSON):
{{ feasibility_json }}

Feasibility Scores (JSON):
{{ feasibility_scores_json }}

Feasibility Analysis (JSON):
{{ feasibility_analysis }}

Feasibility Report (JSON):
{{ feasibility_report }}

Rubrics & Rules (JSON):
{{ feasibility_rules_and_rubrics }}

Pre-Workshop Research (may be truncated):
{{ pre_workshop_data }}

Upcoming Phase (JSON):
{{ next_phase_json }}
"""
    llm = get_chat_llm_pro(
        model_kwargs={
            "temperature": 0.35,
            "max_tokens": 3600,
            "top_k": 40,
            "top_p": 0.9,
            "cache": False,
        }
    )
    chain = PromptTemplate.from_template(prompt_template, template_format="jinja2") | llm
    raw = chain.invoke(inputs)

    text = raw.content if hasattr(raw, "content") else str(raw)
    json_block = extract_json_block(text) or text
    data = json.loads(json_block)
    if not isinstance(data, dict):
        raise ValueError("Model output must be a JSON object.")
    return data

# ---------- API entry ----------
def get_prioritization_payload(
    workshop_id: int,
    previous_task_id: Optional[int] = None,
    phase_context: Optional[str] = None,
) -> Dict[str, Any] | Tuple[str, int]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return "Workshop not found", 404

    try:
        inputs = _prepare_prioritization_inputs(workshop_id, previous_task_id, phase_context)
    except Exception as exc:
        current_app.logger.error("[Prioritization] Input assembly failed: %s", exc, exc_info=True)
        return "Failed to prepare prioritization inputs", 500

    try:
        data = _invoke_prioritization_model(inputs)
    except Exception as exc:
        current_app.logger.error("[Prioritization] LLM error: %s", exc, exc_info=True)
        return "Prioritization generation error", 503

    # Minimal schema check (LLM-owned content otherwise)
    required = ["title", "task_type", "task_description", "instructions", "task_duration",
                "narration", "tts_script", "tts_read_time_seconds", "prioritized", "document_spec"]
    if not all(k in data for k in required):
        return "Prioritization output missing required fields", 500

    try:
        clusters_full = json.loads(inputs.get("clusters_full_json", "[]"))
    except Exception:
        clusters_full = []
    try:
        feasibility_payload = json.loads(inputs.get("feasibility_json", "{}"))
    except Exception:
        feasibility_payload = {}

    _apply_deterministic_overlay(data, ws, clusters_full, feasibility_payload)

    # Persist task with raw LLM JSON (no heuristic fallbacks)
    task = BrainstormTask()
    task.workshop_id = workshop_id
    task.task_type = _safe(data.get("task_type") or "results_prioritization")
    task.title = _safe(data.get("title") or "Prioritization & Shortlisting")
    task.description = _safe(data.get("task_description"))
    task.duration = int(data.get("task_duration") or 900)
    task.status = "pending"
    payload_str = json.dumps(data, ensure_ascii=False)
    task.prompt = payload_str
    task.payload_json = payload_str
    db.session.add(task)
    db.session.flush()
    data["task_id"] = task.id

    # Render PDF from LLM doc spec
    try:
        assets = _render_shortlist_pdf(ws, data.get("document_spec") or {})
        if assets:
            abs_path, rel_path, url = assets
            data["shortlist_pdf_path"] = rel_path
            data["shortlist_pdf_url"] = url
            _attach_doc(ws, abs_path, rel_path, url, data)
            augmented = json.dumps(data, ensure_ascii=False)
            task.payload_json = augmented
            task.prompt = augmented
    except Exception as exc:
        current_app.logger.error("[Prioritization] PDF generation error: %s", exc, exc_info=True)

    current_app.logger.info("[Prioritization] Created task %s for workshop %s", task.id, ws.id)
    return data
