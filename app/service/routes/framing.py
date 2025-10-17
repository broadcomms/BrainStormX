# app/service/routes/framing.py
"""Framing task payload generator and artifact builder.

This module orchestrates a single Bedrock Nova call to produce the complete
framing payload (problem statement, assumptions, constraints, success
criteria, narration, and TTS script) and generates a companion PDF brief for
whiteboard display. The implementation mirrors other AI-powered result phases
(feasibility, prioritization, action plan) and requires a successful LLM
response.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Tuple

from flask import current_app
from botocore.exceptions import ClientError

from app.config import Config
from app.extensions import db
from app.models import BrainstormTask, Workshop, WorkshopPlanItem, Document, WorkshopDocument
from app.tasks.registry import TASK_REGISTRY
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.json_utils import extract_json_block

try:  # pragma: no cover - optional dependency
    from app.utils.llm_bedrock import get_chat_llm, get_chat_llm_pro  # type: ignore
    from langchain_core.prompts import PromptTemplate  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    get_chat_llm = None  # type: ignore
    get_chat_llm_pro = None  # type: ignore
    PromptTemplate = None  # type: ignore



class FramingGenerationError(RuntimeError):
    """Raised when the framing payload cannot be generated from the LLM output."""


def _coerce_to_text(value: Any) -> str:
    """Best-effort conversion of an LLM response to a plain string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:  # pragma: no cover - optional dependency
        from langchain_core.messages import BaseMessage  # type: ignore

        if isinstance(value, BaseMessage):
            content = getattr(value, "content", None)
            if isinstance(content, str):
                return content
    except Exception:  # pragma: no cover
        pass
    if isinstance(value, dict):
        for key in ("content", "text", "generated_text", "message"):
            val = value.get(key)
            if isinstance(val, str):
                return val
    return str(value)


def _require_string_list(raw: Any, *, field_name: str) -> List[str]:
    items: List[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                stripped = entry.strip()
                if stripped:
                    items.append(stripped)
            elif entry is not None:
                text = str(entry).strip()
                if text:
                    items.append(text)
    elif isinstance(raw, str):
        for part in raw.splitlines():
            part = part.strip()
            if part:
                items.append(part)
    if not items:
        raise FramingGenerationError(f"LLM output for '{field_name}' must contain at least one non-empty string.")
    return items


def _compute_read_time_seconds(text: str, *, minimum: int = 45, default: int = 90) -> int:
    try:
        words = len((text or "").split())
        if words <= 0:
            return default
        seconds = int(round(words / 2.3))  # ~150 wpm pacing
        return max(minimum, seconds)
    except Exception:
        return default


def _truncate(text: str, limit: int = 6000) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _reports_dir() -> str:
    base = os.path.join(current_app.instance_path, "uploads", "reports")
    os.makedirs(base, exist_ok=True)
    return base


def _safe_title(title: str) -> str:
    return (title or "Workshop").strip().replace("/", "-")


def _generate_framing_pdf(workshop: Workshop, content: Dict[str, Any]) -> Tuple[str, str, str] | None:
    """Render a polished Framing Brief PDF and return (abs_path, rel_path, url)."""
    try:
        # ReportLab imports
        from reportlab.lib import colors  # type: ignore
        from reportlab.lib.enums import TA_CENTER, TA_LEFT  # type: ignore
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
        )
        from reportlab.pdfgen import canvas  # type: ignore
        from reportlab.platypus.flowables import HRFlowable
    except Exception as exc:
        current_app.logger.warning("[Framing] ReportLab unavailable for PDF generation: %s", exc)
        return None

    # ---------- paths ----------
    abs_dir = _reports_dir()
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S")
    filename = f"{_safe_title(workshop.title)} framing brief {timestamp}.pdf"
    abs_path = os.path.join(abs_dir, filename)
    rel_path = os.path.join("uploads", "reports", filename)

    # ---------- styles ----------
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

    # ---------- helpers ----------
    def rule(height=0.6, color="#E5E7EB", space=8):
        t = Table([[""]], colWidths=[6.5 * inch], rowHeights=[height])
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
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
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

    # ---------- doc with footer ----------
    def _footer(canv: canvas.Canvas, doc):
        canv.setFont("Helvetica", 9)
        canv.setFillColor(colors.HexColor("#6B7280"))
        canv.drawRightString(7.95 * inch, 0.5 * inch, f"Page {doc.page}")

    frame = Frame(0.75 * inch, 0.75 * inch, 7.0 * inch, 9.75 * inch, showBoundary=0)
    doc = BaseDocTemplate(abs_path, pagesize=LETTER, leftMargin=0, rightMargin=0, topMargin=0, bottomMargin=0)
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_footer)])

    # ---------- content assembly ----------
    E: List[Any] = []
    
    E.append(Spacer(1, 14))
    # Title & meta
    E.append(Paragraph(f"{workshop.title}", Title))
    E += rule_2(usable_w,color="#0d6efd")
    E.append(Spacer(1, 4))
    meta_items = [
        f"<b>Scheduled:</b> {meta_value(workshop.date_time.strftime('%Y-%m-%d %H:%M UTC') if workshop.date_time else 'TBD')}",
        f"<b>Duration:</b> {meta_value(workshop.duration, 'TBD')} min",
        f"<b>Participants:</b> {getattr(workshop.participants, 'count', lambda: 0)() if hasattr(workshop.participants, 'count') else len(getattr(workshop, 'participants', []) or [])}",
    ]
    for line in meta_items:
        E.append(Paragraph(line, Small))
    E.append(Spacer(1, 10))
   

    # Opening keynote
    #if content.get("opening_keynote"):
        # E.append(Paragraph("Opening Keynote", Subhead))
        # E.append(Paragraph(content["opening_keynote"], Quote))
        # E += rule()

    # Problem
    E.append(Paragraph("Problem Statement", H1))
    
    E.append(Paragraph(content.get("problem_statement", ""), Quote))
    
    # Success criteria
    sc = content.get("success_criteria") or []
    if sc:
        E.append(Paragraph("Success Criteria", H2))
        E.append(bullet_list(sc))

    # Context
    if content.get("context_summary"):
        E.append(Paragraph("Context", H2))
        E.append(Paragraph(content["context_summary"], Body))

    # Key insights
    ki = content.get("key_insights") or []
    if ki:
        E.append(Paragraph("Key Insights", H2))
        E.append(bullet_list(ki))

    # Participation norms (chips)
    norms = content.get("participation_norms") or []
    if norms:
        E += rule_2(usable_w)
        E.append(Paragraph("Participation", H1))
        E.append(chip_row(norms))

    # Warm-up (segue + prompt)
    if content.get("warmup_segue") or content.get("warmup_instruction"):
        E += rule_2(usable_w)
        E.append(Paragraph("Warm-up prompt", H1))
        if content.get("warmup_instruction"):
            E.append(Paragraph(f"❝ {content['warmup_instruction']} ❞", Quote))

    # Agenda highlights (optional)
    ah = content.get("agenda_highlights") or []
    if ah:
        E.append(Paragraph("Agenda Highlights", H2))
        E.append(bullet_list(ah))

    # Assumptions & Constraints
    asm = content.get("assumptions") or []
    if asm:
        E += rule_2(usable_w, color="#E5E7EB")
        E.append(Paragraph("Assumptions", H1))
        E.append(bullet_list(asm))
    cons = content.get("constraints") or []
    if cons:
        E.append(Paragraph("Constraints", H2))
        E.append(bullet_list(cons))

    # Unknowns (TBD)
    #unk = content.get("unknowns") or []
    #if unk:
    #    E += rule(color="#F59E0B")  # amber
    #    E.append(Paragraph("Unknowns / TBD", H1))
    #    E.append(bullet_list(unk))

    # Footer note
    E += rule_2(usable_w)
    prepared = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    est = content.get("estimated_read_time")
    if isinstance(est, (int, float)):
        E.append(Paragraph(f"Prepared by BrainStormX, {prepared} • Estimated read time: {int(est)}s", Small))
    else:
        E.append(Paragraph(f"Prepared {prepared}", Small))

    # ---------- build ----------
    try:
        doc.build(E)
    except Exception as exc:
        current_app.logger.error("[Framing] PDF build failed: %s", exc, exc_info=True)
        return None

    url = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{os.path.basename(rel_path)}"
    return abs_path, rel_path, url


def _attach_framing_document(
    ws: Workshop,
    *,
    abs_path: str,
    rel_path: str,
    url: str,
    payload: Dict[str, Any],
) -> None:
    try:
        size: int | None
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = None

        title = f"{ws.title} — Framing Brief"
        doc = Document()
        doc.workspace_id = ws.workspace_id
        doc.title = title
        doc.description = "Automatically generated framing brief"
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
        payload["framing_document"] = dict(doc_payload)
        payload["document"] = dict(doc_payload)
    except Exception as exc:  # pragma: no cover - safeguard
        current_app.logger.warning(
            "[Framing] Skipped document attachment for workshop %s: %s",
            ws.id,
            exc,
        )


def _resolve_duration_seconds(cfg: Dict[str, Any]) -> int:
    """Extract duration from config with strict mode support.
    
    Note: strict_mode only applies to LLM-generated content validation,
    not to missing config values (which use registry defaults).
    """
    default_duration = int(TASK_REGISTRY.get("framing", {}).get("default_duration", 600))
    
    raw = cfg.get("duration_sec")
    
    if raw is None:
        # Missing config is normal - use registry default
        current_app.logger.debug(
            "[Framing] duration_sec not in config, using registry default %ds",
            default_duration
        )
        return default_duration
    
    try:
        if isinstance(raw, (int, float)):
            cand = int(raw)
        elif isinstance(raw, str) and raw.strip():
            cand = int(float(raw.strip()))
        else:
            raise ValueError(f"Invalid duration_sec type: {type(raw)}")
        
        # Validate range and clamp if needed
        if not (30 <= cand <= 7200):
            current_app.logger.warning(
                "[Framing] Duration %ds outside valid range (30-7200), clamping",
                cand
            )
            cand = max(30, min(7200, cand))
        
        return cand
        
    except (ValueError, TypeError) as exc:
        current_app.logger.warning(
            "[Framing] Invalid duration_sec '%s': %s, using registry default %ds",
            raw,
            exc,
            default_duration
        )
        return default_duration


def _collect_workshop_overview(ws: Workshop) -> str:
    organizer = getattr(ws, "organizer", None)
    organizer_name = None
    if organizer is not None:
        for attr in ("display_name", "first_name", "email"):
            organizer_name = getattr(organizer, attr, None)
            if organizer_name:
                break
    if not organizer_name:
        organizer_name = "Unknown organizer"

    try:
        participant_count = ws.participants.count() if hasattr(ws.participants, "count") else len(ws.participants)  # type: ignore[arg-type]
    except Exception:
        participant_count = 0

    lines = [
        f"Title: {ws.title}",
        f"Objective: {ws.objective or 'TBD'}",
        f"Organizer: {organizer_name}",
        f"Scheduled: {ws.date_time.strftime('%Y-%m-%d %H:%M UTC') if ws.date_time else 'Not scheduled'}",
        f"Expected Duration: {ws.duration or 'TBD'} minutes",
        f"Participants Invited: {participant_count}",
    ]
    return "\n".join(lines)


def _summarize_plan(ws: Workshop) -> str:
    try:
        plan_items = list(ws.plan_items or [])  # type: ignore[arg-type]
    except Exception:
        plan_items = []
    if not plan_items:
        return "No normalized plan items available."

    current_idx = ws.current_task_index if isinstance(ws.current_task_index, int) else -1
    lines: List[str] = []
    for idx, item in enumerate(plan_items):
        marker = "→" if idx == current_idx else ("✓" if 0 <= current_idx > idx else "•")
        task_label = (item.task_type or "task").replace("_", " ")
        phase = (item.phase or "").strip()
        duration = f"{item.duration or 0}s"
        if phase and phase.lower() != task_label.lower():
            label = f"{task_label} – {phase}"
        else:
            label = task_label
        lines.append(f"{marker} {idx + 1}. {label} ({duration})")
        if len(lines) >= 8:
            break
    return "\n".join(lines)


def _collect_organizer_hints(cfg: Dict[str, Any]) -> str:
    key_points = cfg.get("key_points")
    if isinstance(key_points, list):
        kp_lines = [f"- {str(point).strip()}" for point in key_points if str(point).strip()]
    elif isinstance(key_points, str):
        kp_lines = [f"- {line.strip()}" for line in key_points.splitlines() if line.strip()]
    else:
        kp_lines = []
    hints = [
        f"Framing prompt: {cfg.get('framing_prompt') or 'None provided'}",
        f"Preferred style: {cfg.get('style') or 'Concise, inclusive, motivating'}",
        f"Audience: {cfg.get('audience') or 'Workshop participants'}",
    ]
    if kp_lines:
        hints.append("Key points:")
        hints.extend(kp_lines)
    return "\n".join(hints)


def _gather_prompt_inputs(ws: Workshop, cfg: Dict[str, Any], phase_context: str | None) -> Dict[str, str]:
    overview = _collect_workshop_overview(ws)
    hints = _collect_organizer_hints(cfg)
    plan_snapshot = _summarize_plan(ws)
    try:
        prework = get_pre_workshop_context_json(ws.id)
    except Exception:
        prework = ""
    return {
        "workshop_overview": overview,
        "organizer_hints": hints,
        "agenda_snapshot": plan_snapshot,
        "prework_data": _truncate(prework, 5500),
        "phase_context": (phase_context or "").strip() or "Briefing",
    }

def _invoke_framing_model(
    ws: Workshop,
    inputs: Dict[str, str],
) -> Dict[str, Any]:
    if get_chat_llm_pro is None:
        raise FramingGenerationError("LLM not available for framing generation.")
    if PromptTemplate is None:
        raise FramingGenerationError("Prompt template tooling not available for framing generation.")

    template = """
You are the expert workshop facilitator and technical writer. 
Based *only* on the workshop context and information provided for this session. 
Produce the complete framing content for a brainstorming workshop.
Respond with ONE strict JSON object. (no markdown, no commentary) that contains:
  - opening_keynote: string - short headline sententence to set the tone for the workshop
  - problem_statement: string (concise, 1-4 sentences, grounded in the context)
  - warmup_segue: string - one sentence that naturally hands off to warm-up
  - warmup_instruction: string - the exact, warm-up question/prompt the facilitator will ask.
  - participation_norms: string[] - 3-5 brief bullet points describing collaboration norms
  - agenda_highlights: string[] - 3-6 bullet points summarizing the upcoming tasks 
  - assumptions: array of 3-5 short strings
  - constraints: array of 3-5 short strings
  - tech_feasibility_rubric: string[] - if applicable, a rubric description of how technical feasibility will be evaluated
  - legal_compliance_rules: string[] - if applicable, a rule template description of how legal requirements and compliance will be evaluated
  - budget_feasibility_rubric: string[] - if applicable, a rubric description of how budget feasibility will be evaluated
  - data_privacy_checklist: string[] - if applicable, a checklist of data privacy considerations
  - ethical_considerations: string[] - if applicable, a checklist of ethical considerations
  - success_criteria: array of 3-5 short strings describing measurable outcomes
  - context_summary: string (<= 120 words, no bullets)
  - key_insights: array of up to 3-5 short bullet phrases highlighting critical context
  - framing_narration: string ( A single paragraph the facilitator would speak aloud)
  - unknowns: string[], - if any required information is missing or unknown, state "TBD"
  - tts_script: string (110-220 words, a single paragraph with naturally weaves the flow to open the workshop, suitable for text-to-speech. 
        Avoid lists, quotation marks around entire sentences, and avoid JSON-breaking characters.
  - estimated_read_time: integer seconds approximating the tts_script read time
  - read_speech_rate_wpm: number - recommended speech rate in words per minute for reading the tts_script aloud so you can compute estimated_read_time
  
  
Style & Constraints:
- Tone: Human, neutral, Warm, professional, accissible, encouraging, inclusive and motivating; first-person facilitator voice.
- Accuracy: Strictly adhere to the provided context and details. Do not invent or assume facts. Do not fabricate details; use "TBD" where necessary.
- Flow: Opening → overview → problem → audience → assumptions → constraints → success criteria & expectations.
- Pacing cues: Keep minimal and human-friendly.
- Length: 110–220 words total. No bullet points, or numbered lists. Use words to convery numbers like 1 to 20, not "1-20" or "1–20".
- Strict JSON: No trailing commas, no extra keys, no markdown, no code fences.

USE ONLY THE INFORMATION PROVIDED BELOW. If any required information is missing or unknown, state "TBD".

Workshop Overview:
{workshop_overview}

Organizer Guidance:
{organizer_hints}

Phase Context:
{phase_context}

Upcoming Tasks Snapshot:
{agenda_snapshot}

Pre-Workshop Data Summary:
{prework_data}


Hard Rules:
  - Use ONLY the information provided. If a detail is unknown, label it "TBD".
  - Keep tone inclusive, motivational, and grounded.
  - Do not include markdown, code fences, or trailing commas. Return JSON only.
  - Do not start the warm up activity, stop speech after success criteria.
  - All required fields must be present and non-empty.
  - context_summary <= 120 words; framing_narration 150–300 words; tts_script 110–220 words.
  - estimated_read_time is integer >= 45.
  - Arrays (assumptions, constraints, success_criteria) contain 3–7 items; key_insights 3–6.
  - If warmup_instruction is present, it is one sentence and actionable.

"""

    try:
        llm = get_chat_llm_pro(
            model_kwargs={
                "temperature": 0.3,
                "max_tokens": 3200,
                "top_k": 40,
                "top_p": 0.9,
            }
        )
        prompt = PromptTemplate.from_template(template)
        chain = prompt | llm
        raw_response = chain.invoke(inputs)
        text = _coerce_to_text(raw_response)
        json_block = extract_json_block(text) or text
        data = json.loads(json_block)
        if not isinstance(data, dict):
            raise FramingGenerationError("Model output was not a JSON object.")
        return data
    except FramingGenerationError:
        raise
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code") if hasattr(exc, "response") else None
        if error_code == "ServiceUnavailableException":
            current_app.logger.warning("[Framing] Rate limited by upstream LLM", exc_info=True)
            return {
                "problem_statement": "Unable to generate the framing brief right now due to heavy traffic.",
                "context_summary": "Please retry shortly so we can compile the framing context.",
                "framing_narration": "We're temporarily rate limited by the LLM provider. Reload in a few seconds and try again.",
                "tts_script": "We couldn't narrate the framing brief because of provider traffic. Please try again soon.",
                "assumptions": ["TBD"],
                "constraints": ["TBD"],
                "success_criteria": ["TBD"],
                "key_insights": ["Assistant is currently rate limited."],
                "estimated_read_time": 60,
            }
        current_app.logger.error("[Framing] LLM generation failed: %s", exc, exc_info=True)
        raise FramingGenerationError(f"Framing generation error: {exc}") from exc
    except Exception as exc:  # pragma: no cover - runtime safety
        current_app.logger.error("[Framing] LLM generation failed: %s", exc, exc_info=True)
        raise FramingGenerationError(f"Framing generation error: {exc}") from exc


def _normalize_content(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise FramingGenerationError("LLM output must be a JSON object.")

    def _require_str(key: str) -> str:
        val = raw.get(key)
        if not isinstance(val, str) or not val.strip():
            raise FramingGenerationError(f"LLM output for '{key}' must be a non-empty string.")
        return val.strip()

    content: Dict[str, Any] = {
        "problem_statement": _require_str("problem_statement"),
        "context_summary": _require_str("context_summary"),
        "framing_narration": _require_str("framing_narration"),
        "tts_script": _require_str("tts_script"),
    }

    for field in ("assumptions", "constraints", "success_criteria", "key_insights"):
        content[field] = _require_string_list(raw.get(field), field_name=field)

    # Optional enrichers
    for opt in ("opening_keynote","warmup_segue","warmup_instruction"):
        if isinstance(raw.get(opt), str) and raw[opt].strip():
            content[opt] = raw[opt].strip()
    for opt_arr in ("participation_norms","agenda_highlights","unknowns"):
        if raw.get(opt_arr):
            try:
                content[opt_arr] = _require_string_list(raw.get(opt_arr), field_name=opt_arr)
            except FramingGenerationError:
                pass  # optional

    if isinstance(raw.get("read_speech_rate_wpm"), (int, float)):
        content["read_speech_rate_wpm"] = float(raw["read_speech_rate_wpm"])

    estimated = raw.get("estimated_read_time")
    if estimated is not None:
        try:
            content["estimated_read_time"] = max(45, int(round(float(estimated))))
        except Exception as exc:
            raise FramingGenerationError("LLM output for 'estimated_read_time' must be numeric.") from exc
    else:
        content["estimated_read_time"] = _compute_read_time_seconds(content["tts_script"])

    return content


def _create_payload(
    ws: Workshop,
    cfg: Dict[str, Any],
    phase_context: str | None,
    duration: int,
    cc_enabled: bool,
    content: Dict[str, Any],
    *,
    preview: bool = False,
) -> Dict[str, Any]:
    title = cfg.get("title") or "Workshop Briefing"
    description = cfg.get("task_description") or "Frame the session with the objective, constraints, and success metrics; deliver a 90–120s opening; confirm participation norms; and set up the warm-up segue."
    instructions = cfg.get("instructions") or "Share the framing brief, read the opening, confirm norms (one idea per turn, stay on topic, keep it brief), and display success criteria. Then cue the warm-up prompt."

    resolved_phase_context = "Briefing"
    if isinstance(phase_context, str):
        cleaned = phase_context.strip()
        if cleaned and cleaned.lower() == "briefing":
            resolved_phase_context = "Briefing"

    facilitator_panel: Dict[str, Any] = {
        "task_title": title,
        "workshop_title": ws.title,
        "opening_keynote": content.get("opening_keynote") or title,
        "problem_statement": content.get("problem_statement"),
        "success_criteria": content.get("success_criteria"),
        "participation_norms": content.get("participation_norms", [
            "One idea per turn",
            "Be concise",
            "Build on others",
            "Assume good intent",
        ]),
        "warmup_segue": content.get("warmup_segue", ""),
        "warmup_instruction": content.get("warmup_instruction", ""),
    }
    if content.get("agenda_highlights"):
        facilitator_panel["agenda_highlights"] = content["agenda_highlights"]
    if content.get("unknowns"):
        facilitator_panel["unknowns"] = content["unknowns"]

    payload: Dict[str, Any] = {
        "title": title,
        "task_type": "framing_preview" if preview else "framing",
        "task_description": description,
        "instructions": instructions,
        "task_duration": duration,
        "cc_enabled": cc_enabled,
        "delivery_mode": "framing",
        "phase_context": resolved_phase_context,
        "problem_statement": content.get("problem_statement"),
        "assumptions": content.get("assumptions"),
        "constraints": content.get("constraints"),
        "success_criteria": content.get("success_criteria"),
        "context_summary": content.get("context_summary"),
        "key_insights": content.get("key_insights"),
        "framing_narration": content.get("framing_narration"),
        "narration": content.get("framing_narration"),
        "tts_script": content.get("tts_script"),
        "tts_read_time_seconds": content.get("estimated_read_time"),
        "estimated_read_time": content.get("estimated_read_time"),
        "facilitator_panel": facilitator_panel,
    }
    return payload


def _persist_task(ws: Workshop, payload: Dict[str, Any]) -> BrainstormTask:
    task = BrainstormTask()
    task.workshop_id = ws.id
    task.task_type = "framing"
    task.title = payload.get("title")
    task.description = payload.get("task_description")
    task.duration = int(payload.get("task_duration", 600))
    task.status = "pending"
    db.session.add(task)
    db.session.flush()
    payload["task_id"] = task.id
    payload_json = json.dumps(payload)
    task.prompt = payload_json
    task.payload_json = payload_json
    return task


def _build_payload(
    ws: Workshop,
    cfg: Dict[str, Any],
    phase_context: str | None,
    *,
    persist_task: bool,
    include_pdf: bool,
    preview: bool = False,
) -> Dict[str, Any]:
    duration = _resolve_duration_seconds(cfg)
    cc_enabled = bool(cfg.get("cc_enabled", True))
    prompt_inputs = _gather_prompt_inputs(ws, cfg, phase_context)
    raw_content = _invoke_framing_model(ws, prompt_inputs)
    content = _normalize_content(raw_content)

    payload = _create_payload(
        ws,
        cfg,
        phase_context,
        duration,
        cc_enabled,
        content,
        preview=preview,
    )

    if include_pdf:
        pdf_assets = _generate_framing_pdf(ws, content)
        if pdf_assets:
            abs_path, rel_path, url = pdf_assets
            payload["framing_pdf_path"] = rel_path
            payload["framing_pdf_url"] = url
            payload["pdf_document"] = url
            _attach_framing_document(
                ws,
                abs_path=abs_path,
                rel_path=rel_path,
                url=url,
                payload=payload,
            )

    if persist_task:
        task = _persist_task(ws, payload)
        current_app.logger.info("[Framing] Created task %s for workshop %s", task.id, ws.id)
    return payload


def get_framing_payload(workshop_id: int, phase_context: str | None = None):
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return "Workshop not found", 404

    cfg = _get_plan_item_config(workshop_id, "framing") or {}
    try:
        payload = _build_payload(
            ws,
            cfg,
            phase_context,
            persist_task=True,
            include_pdf=True,
            preview=False,
        )
    except FramingGenerationError as exc:
        current_app.logger.error("[Framing] Unable to generate framing payload: %s", exc)
        return str(exc), 503
    return payload


def build_framing_preview(workshop_id: int, cfg: Dict[str, Any], phase_context: str | None = None) -> Dict[str, Any]:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return {"error": "Workshop not found"}
    cfg = cfg or {}
    try:
        payload = _build_payload(
            ws,
            cfg,
            phase_context,
            persist_task=False,
            include_pdf=False,
            preview=True,
        )
    except FramingGenerationError as exc:
        current_app.logger.error("[Framing] Unable to generate preview payload: %s", exc)
        return {"error": str(exc)}
    return payload


def _legacy_speech_framing_config(workshop_id: int, current_idx: int) -> Dict[str, Any] | None:
    """Gracefully handle pre-refactor speech items set to framing delivery mode."""
    try:
        q = (
            WorkshopPlanItem.query
            .filter_by(workshop_id=workshop_id, task_type="speech", enabled=True)
            .order_by(WorkshopPlanItem.order_index.asc())
        )
        for item in q.all():
            try:
                if item.order_index is not None and int(item.order_index) <= current_idx:
                    continue
            except Exception:
                pass
            raw: Any = None
            if getattr(item, "config_json", None):
                raw = item.config_json
            elif getattr(item, "description", None):
                raw = item.description
            if not raw:
                continue
            try:
                cfg = json.loads(raw) if not isinstance(raw, dict) else raw  # type: ignore[arg-type]
            except Exception:
                continue
            if isinstance(cfg, dict) and (cfg.get("delivery_mode") or "").strip().lower() == "framing":
                return {k: v for k, v in cfg.items() if k != "delivery_mode"}
    except Exception:
        return None
    return None


def _get_plan_item_config(workshop_id: int, ttype: str) -> Dict[str, Any] | None:
    """Return config for next matching plan item, prefer JSON config fields."""
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
            if getattr(item, "config_json", None):
                try:
                    data = json.loads(item.config_json) if not isinstance(item.config_json, dict) else item.config_json  # type: ignore[arg-type]
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
            if getattr(item, "description", None):
                try:
                    data = json.loads(item.description)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return None
    except Exception:
        return None
    if ttype == "framing":
        legacy = _legacy_speech_framing_config(workshop_id, current_idx)
        if legacy:
            current_app.logger.debug("[Framing] Falling back to legacy speech(plan) config for workshop %s", workshop_id)
        return legacy
    return None
