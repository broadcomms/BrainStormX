from __future__ import annotations

import json
import re
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from flask import current_app
from langchain.schema import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import Config
from app.extensions import db
from app.models import Document, LLMUsageLog, Workshop, WorkshopAgenda, WorkshopDocument
from app.utils.data_aggregation import aggregate_pre_workshop_data
from app.utils.json_utils import extract_json_block
from app.utils.llm_bedrock import get_chat_llm
from app.utils.context_models import WorkspaceContextBundle
from sqlalchemy.orm import joinedload

ALLOWED_TASK_TYPES = {
    "framing",
    "warm_up",
    "brainstorming",
    "clustering_voting",
    "feasibility",
    "prioritization",
    "discussion",
    "action_planning",
    "summary",
    "presentation",
    "meeting",
}

TASK_TYPE_ALIASES: Dict[str, str] = {
    "warm up": "warm_up",
    "warm-up": "warm_up",
    "warmup": "warm_up",
    "icebreaker": "warm_up",
    "ice breaker": "warm_up",
    "kickoff": "framing",
    "kick-off": "framing",
    "activity": "brainstorming",
    "activities": "brainstorming",
    "context setting": "framing",
    "introduction": "framing",
    "intro": "framing",
    "group activity": "brainstorming",
    "group activities": "brainstorming",
    "group work": "brainstorming",
    "co-creation": "brainstorming",
    "co creation": "brainstorming",
    "ideation": "brainstorming",
    "ideation burst": "brainstorming",
    "ideation sprint": "brainstorming",
    "brainstorm": "brainstorming",
    "brainstorm session": "brainstorming",
    "affinity clustering": "clustering_voting",
    "affinity mapping": "clustering_voting",
    "affinity map": "clustering_voting",
    "cluster": "clustering_voting",
    "clustering": "clustering_voting",
    "dot voting": "clustering_voting",
    "dot-voting": "clustering_voting",
    "voting": "clustering_voting",
    "vote": "clustering_voting",
    "feasibility scan": "feasibility",
    "feasibility assessment": "feasibility",
    "feasibility review": "feasibility",
    "impact effort": "prioritization",
    "impact/effort": "prioritization",
    "prioritisation": "prioritization",
    "prioritise": "prioritization",
    "prioritize": "prioritization",
    "ranking": "prioritization",
    "action plan": "action_planning",
    "action planning": "action_planning",
    "next steps": "action_planning",
    "commitments": "action_planning",
    "planning": "action_planning",
    "wrap up": "summary",
    "wrap-up": "summary",
    "closing": "summary",
    "close": "summary",
    "recap": "summary",
    "share out": "discussion",
    "share-out": "discussion",
    "shareback": "discussion",
    "q&a": "discussion",
    "qa": "discussion",
}

TASK_TYPE_KEYWORD_RULES: Sequence[tuple[str, Sequence[str]]] = (
    ("warm_up", ("warm", "icebreaker", "ice breaker")),
    ("framing", ("context", "kickoff", "kick-off", "introduction", "intro")),
    ("brainstorming", ("brainstorm", "ideation", "co-creation", "group activity", "divergent")),
    ("clustering_voting", ("cluster", "affinity", "vote", "scoring", "categorize")),
    ("feasibility", ("feasibility", "viability", "evaluation")),
    ("prioritization", ("priorit", "impact", "ranking", "scorecard")),
    ("action_planning", ("action", "plan", "commit", "roadmap", "next step")),
    ("discussion", ("discussion", "debrief", "share", "q&a", "panel")),
    ("summary", ("summary", "wrap", "closing", "recap", "final")),
)


def _canonicalize_task_type(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("task_type must be a string")

    raw = value.strip().lower()
    if not raw:
        raise ValueError("task_type cannot be empty")

    normalized_underscore = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    normalized_space = re.sub(r"[^a-z0-9]+", " ", raw).strip()
    collapsed_space = re.sub(r"\s+", " ", normalized_space)

    candidates = [
        normalized_underscore,
        collapsed_space.replace(" ", "_"),
        collapsed_space,
        raw,
    ]

    for candidate in candidates:
        if candidate in ALLOWED_TASK_TYPES:
            return candidate

    for candidate in candidates:
        alias = TASK_TYPE_ALIASES.get(candidate)
        if alias:
            return alias

    for canonical, keywords in TASK_TYPE_KEYWORD_RULES:
        for keyword in keywords:
            if keyword in raw:
                return canonical

    raise ValueError(
        f"Invalid task_type '{value}'. Must be one of {sorted(ALLOWED_TASK_TYPES)}"
    )

DEFAULT_ORIGIN = "llm"
CONTEXT_CHAR_LIMIT = 6000
DURATION_TOLERANCE_MINUTES = 20

class AgendaGenerationError(Exception):
    """Raised when the agenda pipeline cannot complete successfully."""

    def __init__(
        self,
        message: str,
        *,
        details: Optional[Any] = None,
        code: Optional[str] = None,
    ):
        super().__init__(message)
        self.details = details
        self.code = code

@dataclass
class NormalizedAgendaItem:
    position: int
    title: str
    description: str
    task_type: str
    duration_minutes: int
    start_offset_minutes: int
    end_offset_minutes: int
    notes: Optional[str]
    origin: str




@dataclass
class AgendaPipelineResult:
    agenda_json: Optional[str]
    items: List[NormalizedAgendaItem]
    guidelines: List[str]
    icebreaker: Optional[str]
    facilitator_tips: List[str]
    executive_summary: Optional[str]
    confidence_level: Optional[str]
    related_documents: List[Dict[str, Any]]
    draft_documents: List[Dict[str, Any]]
    context_snapshot: Optional[Dict[str, Any]]


def _summarize_documents_for_prompt(documents: Sequence[Any], *, max_documents: int = 4) -> str:
    if not documents:
        return "(no linked documents)"

    lines: List[str] = []
    for doc in list(documents)[:max_documents]:
        title = getattr(doc, "title", None) or "Untitled Document"
        excerpt = _safe_trim(
            getattr(doc, "excerpt", None)
            or getattr(doc, "summary", None)
            or getattr(doc, "description", None),
            max_length=420,
        )
        base_line = f"- {title}"
        if excerpt:
            base_line += f": {excerpt}"
        lines.append(base_line)

        highlights = getattr(doc, "highlights", None) or []
        for highlight in list(highlights)[:3]:
            trimmed = _safe_trim(highlight, max_length=280)
            if trimmed:
                lines.append(f"    • {trimmed}")

    remaining = len(documents) - max_documents
    if remaining > 0:
        lines.append(f"- …and {remaining} more documents linked")

    return "\n".join(lines)


def _build_context_metadata(context_bundle: WorkspaceContextBundle) -> Dict[str, Any]:
    payload = context_bundle.payload
    metadata: Dict[str, Any] = {
        "version": payload.version,
        "collected_at_iso": payload.collected_at_iso,
        "participant_count": len(payload.participants),
        "document_count": len(payload.documents),
        "agenda_items_count": len(payload.agenda.items or []),
    }
    if payload.workspace:
        metadata["workspace_id"] = payload.workspace.id
        metadata["workspace_name"] = payload.workspace.name
    return metadata


def _context_doc_to_dict(context_document: Any) -> Dict[str, Any]:
    data = context_document.model_dump(exclude_none=True)
    highlights = data.get("highlights") or []
    if highlights:
        data["highlights"] = [
            _safe_trim(str(item), max_length=280) or str(item)
            for item in highlights
            if isinstance(item, str)
        ]
    return data

def _safe_trim(value: Optional[str], *, max_length: int) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def _persist_manual_agenda(workshop: Workshop, agenda_draft: str) -> AgendaPipelineResult:
    WorkshopAgenda.query.filter_by(workshop_id=workshop.id).delete()
    db.session.flush()

    cleaned = (agenda_draft or "").strip()
    workshop.agenda_json = None
    workshop.agenda_generated_source = "manual"
    workshop.agenda_generated_at = datetime.utcnow()
    workshop.agenda_auto_generate = False
    workshop.agenda_draft_plaintext = cleaned or None
    workshop.facilitator_guidelines = None
    workshop.facilitator_tips = None
    workshop.facilitator_summary = None
    workshop.agenda_confidence = None
    workshop.agenda = cleaned or None

    db.session.flush()

    return AgendaPipelineResult(
        agenda_json=None,
        items=[],
        guidelines=[],
        icebreaker=None,
        facilitator_tips=[],
        executive_summary=None,
        confidence_level=None,
        related_documents=[],
        draft_documents=[],
        context_snapshot=None,
    )


def _normalize_llm_response(response: Any) -> str:
    if hasattr(response, "content"):
        return str(response.content)
    if isinstance(response, dict) and "content" in response:
        return str(response["content"])
    return str(response)

def _prepare_payload_json(raw_json: str, workshop: Workshop) -> str:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise AgendaGenerationError("The language model response could not be parsed as JSON.") from exc

    if isinstance(data, list):
        current_app.logger.info(
            "Agenda pipeline received top-level array; wrapping into payload object.")
        data = {"agenda": data}

    if isinstance(data, dict):
        if "agenda" not in data:
            item_keys = {"position", "title", "description", "task_type", "duration_minutes"}
            if item_keys.issubset(set(data.keys())):
                current_app.logger.info(
                    "Agenda pipeline received standalone agenda item; coercing into agenda list.")
                data = {"agenda": [data]}
            elif "agenda_items" in data and "agenda" not in data:
                current_app.logger.info(
                    "Agenda pipeline received 'agenda_items' key; normalizing to 'agenda'.")
                data["agenda"] = data.pop("agenda_items")

        if "agenda" not in data:
            raise AgendaGenerationError("Generated agenda payload is missing required 'agenda' field.")

        if not isinstance(data["agenda"], list):
            raise AgendaGenerationError("Generated agenda 'agenda' field is not a list.")

        data.setdefault("workshop_title", workshop.title)
        data.setdefault("objective", workshop.objective)
        if getattr(workshop, "date_time", None) is not None:
            data.setdefault("scheduled_start_iso", workshop.date_time.isoformat())
        if workshop.duration:
            data.setdefault("planned_duration_minutes", workshop.duration)

        return json.dumps(data, ensure_ascii=False)

    raise AgendaGenerationError("Generated agenda payload has unsupported JSON structure.")


def _normalize_items(payload: AgendaPayloadModel) -> List[NormalizedAgendaItem]:
    normalized: List[NormalizedAgendaItem] = []
    running_offset = 0
    total_duration = 0

    if not payload.agenda:
        raise AgendaGenerationError("Agenda items cannot be empty.")

    for idx, item in enumerate(payload.agenda, start=1):
        duration = item.duration_minutes
        if duration is None:
            raise AgendaGenerationError(f"Agenda item {idx} is missing duration_minutes.")

        start_offset = item.start_offset_minutes
        if start_offset is None:
            start_offset = running_offset

        end_offset = item.end_offset_minutes
        if end_offset is None:
            end_offset = start_offset + duration

        if end_offset < start_offset:
            raise AgendaGenerationError(
                f"Agenda item {idx} has end_offset_minutes earlier than start_offset_minutes."
            )

        running_offset = end_offset
        position = item.position or idx
        total_duration += duration

        normalized.append(
            NormalizedAgendaItem(
                position=position,
                title=item.title,
                description=item.description,
                task_type=item.task_type,
                duration_minutes=duration,
                start_offset_minutes=start_offset,
                end_offset_minutes=end_offset,
                notes=item.notes.strip() if isinstance(item.notes, str) else None,
                origin=item.origin or DEFAULT_ORIGIN,
            )
        )

    if payload.planned_duration_minutes:
        tolerance = payload.planned_duration_minutes + DURATION_TOLERANCE_MINUTES
        if total_duration > tolerance:
            raise AgendaGenerationError(
                "Agenda duration exceeds planned workshop duration tolerance.")

    return normalized


def _build_canonical_payload(
    workshop: Workshop,
    payload: AgendaPayloadModel,
    items: Sequence[NormalizedAgendaItem],
    *,
    related_documents: Sequence[Dict[str, Any]],
    draft_documents: Sequence[Dict[str, Any]],
    context_snapshot: Optional[Dict[str, Any]],
) -> dict:
    scheduled_iso = (
        workshop.date_time.isoformat()
        if getattr(workshop, "date_time", None) is not None
        else payload.scheduled_start_iso
    )

    return {
        "workshop_title": payload.workshop_title or workshop.title,
        "objective": payload.objective or workshop.objective,
        "scheduled_start_iso": scheduled_iso,
        "planned_duration_minutes": payload.planned_duration_minutes or workshop.duration,
        "agenda": [
            {
                "position": item.position,
                "title": item.title,
                "description": item.description,
                "task_type": item.task_type,
                "duration_minutes": item.duration_minutes,
                "start_offset_minutes": item.start_offset_minutes,
                "end_offset_minutes": item.end_offset_minutes,
                "notes": item.notes,
                "origin": item.origin,
            }
            for item in items
        ],
        "guidelines": payload.guidelines,
        "icebreaker": payload.icebreaker,
        "facilitator_tips": payload.facilitator_tips,
        "executive_summary": payload.executive_summary,
        "confidence_level": payload.confidence_level,
        "related_documents": list(related_documents),
        "draft_documents": list(draft_documents),
        "context_snapshot": context_snapshot,
    }

class AgendaItemModel(BaseModel):
    position: Optional[int] = Field(default=None, ge=1)
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    task_type: str = Field(..., min_length=1)
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=720)
    start_offset_minutes: Optional[int] = Field(default=None, ge=0)
    end_offset_minutes: Optional[int] = Field(default=None, ge=0)
    notes: Optional[str] = None
    origin: str = Field(default=DEFAULT_ORIGIN)

    @field_validator("title", "description", mode="before")
    def _strip_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("task_type")
    def _validate_task_type(cls, value: str) -> str:
        normalized = _canonicalize_task_type(value)
        if normalized not in ALLOWED_TASK_TYPES:
            raise ValueError(f"Invalid task_type '{value}'. Must be one of {sorted(ALLOWED_TASK_TYPES)}")
        return normalized

    @field_validator("origin", mode="before")
    def _normalize_origin(cls, value: Optional[str]) -> str:
        if not value:
            return DEFAULT_ORIGIN
        return str(value).strip().lower()

class AgendaPayloadModel(BaseModel):
    workshop_title: Optional[str] = None
    objective: Optional[str] = None
    scheduled_start_iso: Optional[str] = None
    planned_duration_minutes: Optional[int] = Field(default=None, ge=10, le=720)
    agenda: List[AgendaItemModel]
    guidelines: List[str] = Field(default_factory=list)
    icebreaker: Optional[str] = None
    facilitator_tips: List[str] = Field(default_factory=list)
    executive_summary: Optional[str] = None
    confidence_level: Optional[str] = None

    @field_validator("guidelines", "facilitator_tips", mode="before")
    def _ensure_list(cls, value: Optional[Any]) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()]

    @field_validator("confidence_level", mode="before")
    def _normalize_confidence(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        normalized = str(value).strip().lower()
        if normalized not in {"high", "medium", "low"}:
            return None
        return normalized

def _collect_related_documents(
    workshop: Workshop,
    context_bundle: Optional[WorkspaceContextBundle],
) -> List[Dict[str, Any]]:
    context_docs: Dict[int, Dict[str, Any]] = {}
    context_only_docs: List[Dict[str, Any]] = []
    if context_bundle:
        for doc_model in context_bundle.payload.documents:
            doc_dict = _context_doc_to_dict(doc_model)
            doc_id = doc_dict.get("id")
            if isinstance(doc_id, int):
                context_docs[int(doc_id)] = doc_dict
            else:
                context_only_docs.append(doc_dict)

    links: Sequence[WorkshopDocument] = (
        WorkshopDocument.query.options(
            joinedload(WorkshopDocument.document).joinedload(Document.uploader)  # type: ignore[arg-type]
        )
        .filter(WorkshopDocument.workshop_id == workshop.id)
        .order_by(WorkshopDocument.added_at.asc())
        .all()
    )

    related: List[Dict[str, Any]] = []

    for link in links:
        doc = link.document
        if not doc:
            continue
        context_data = context_docs.pop(doc.id, None)
        related.append(_merge_document_metadata(doc, context_data, link))

    # Append any context-only documents that did not have direct links
    for context_data in context_docs.values():
        related.append(_merge_document_metadata(None, context_data, None))

    related.extend(context_only_docs)
    return related

def _merge_document_metadata(
    document: Optional[Document],
    context_data: Optional[Dict[str, Any]],
    link: Optional[WorkshopDocument],
) -> Dict[str, Any]:
    context_data = context_data or {}

    def _first_non_empty(*values: Optional[Any]) -> Optional[Any]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value not in (None, "", [], {}):
                return value
        return None

    highlights_raw = context_data.get("highlights") or []
    highlights: List[str] = []
    for item in highlights_raw:
        if isinstance(item, str):
            trimmed = _safe_trim(item, max_length=280)
            if trimmed:
                highlights.append(trimmed)

    uploaded_at = getattr(document, "uploaded_at", None)
    link_added_at = getattr(link, "added_at", None)

    entry: Dict[str, Any] = {
        "id": getattr(document, "id", None) or context_data.get("id"),
        "title": _first_non_empty(
            context_data.get("title"), getattr(document, "title", None), "Untitled Document"
        ),
        "description": _safe_trim(
            _first_non_empty(context_data.get("description"), getattr(document, "description", None)),
            max_length=360,
        ),
        "summary": _safe_trim(
            _first_non_empty(context_data.get("summary"), getattr(document, "summary", None)),
            max_length=600,
        ),
        "excerpt": _safe_trim(context_data.get("excerpt"), max_length=600),
        "source": context_data.get("source") or ("linked" if document else "context"),
        "highlights": highlights,
        "file_name": getattr(document, "file_name", None),
        "file_path": getattr(document, "file_path", None),
        "file_size": getattr(document, "file_size", None),
        "workspace_id": getattr(document, "workspace_id", None),
        "uploaded_at": uploaded_at.isoformat(timespec="seconds") if uploaded_at else None,
        "processing_status": getattr(document, "processing_status", None),
        "content_available": bool(getattr(document, "content", None)),
        "link_added_at": link_added_at.isoformat(timespec="seconds") if link_added_at else None,
        "uploaded_by": _serialize_user(getattr(document, "uploader", None)),
        "document_link_id": getattr(link, "id", None),
        "context_only": document is None,
    }

    if entry.get("file_path") and not entry.get("download_path"):
        entry["download_path"] = entry["file_path"]

    return entry


def _serialize_user(user: Optional[Any]) -> Optional[Dict[str, Any]]:
    if not user:
        return None
    display_name = getattr(user, "display_name", None)
    if not display_name:
        first = (getattr(user, "first_name", "") or "").strip()
        last = (getattr(user, "last_name", "") or "").strip()
        combined = f"{first} {last}".strip()
        display_name = combined or getattr(user, "email", None) or f"User {getattr(user, 'user_id', 'unknown')}"
    return {
        "user_id": getattr(user, "user_id", None),
        "name": display_name,
        "email": getattr(user, "email", None),
    }

def _persist_to_database(
    workshop: Workshop,
    agenda_json: Optional[str],
    items: Sequence[NormalizedAgendaItem],
    guidelines: Sequence[str],
    icebreaker: Optional[str],
    facilitator_tips: Sequence[str],
    executive_summary: Optional[str],
    confidence_level: Optional[str],
    agenda_draft: str,
) -> None:
    workshop.agenda_json = agenda_json
    workshop.agenda_generated_at = datetime.utcnow()
    workshop.agenda_generated_source = DEFAULT_ORIGIN
    workshop.agenda_auto_generate = True
    workshop.agenda_draft_plaintext = (agenda_draft or "").strip() or None
    workshop.facilitator_guidelines = "\n".join(guidelines).strip() or None
    workshop.facilitator_tips = "\n".join(facilitator_tips).strip() or None
    workshop.facilitator_summary = (executive_summary or "").strip() or None
    workshop.agenda_confidence = confidence_level

    display_items: list[dict[str, Any]] = []
    for item in items:
        display_items.append(
            {
                "position": item.position,
                "activity": item.title,
                "title": item.title,
                "description": item.description,
                "estimated_duration": item.duration_minutes,
                "task_type": item.task_type,
                "time_slot": _format_time_slot(item.start_offset_minutes, item.end_offset_minutes),
                "start_offset_minutes": item.start_offset_minutes,
                "end_offset_minutes": item.end_offset_minutes,
                "origin": item.origin,
            }
        )

    workshop.agenda = json.dumps({"agenda": display_items}, ensure_ascii=False) if display_items else agenda_draft

    WorkshopAgenda.query.filter_by(workshop_id=workshop.id).delete()
    db.session.flush()

    for item in items:
        agenda_row = WorkshopAgenda()
        agenda_row.workshop_id = workshop.id
        agenda_row.position = item.position
        agenda_row.activity_title = item.title
        agenda_row.activity_description = item.description
        agenda_row.estimated_duration = item.duration_minutes
        agenda_row.generated_source = DEFAULT_ORIGIN
        agenda_row.start_offset = item.start_offset_minutes * 60
        agenda_row.end_offset = item.end_offset_minutes * 60
        agenda_row.time_slot = _format_time_slot(item.start_offset_minutes, item.end_offset_minutes)
        agenda_row.task_type = item.task_type
        agenda_row.origin = item.origin
        agenda_row.duration_minutes = item.duration_minutes
        db.session.add(agenda_row)

    db.session.flush()


def _format_time_slot(start_minute: int, end_minute: int) -> str:
    def _minutes_to_hhmm(minutes: int) -> str:
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"

    return f"{_minutes_to_hhmm(start_minute)} - {_minutes_to_hhmm(end_minute)}"


def _record_usage_log(*, workshop_id: int, prompt_chars: int, response_chars: int, latency_ms: int) -> None:
    try:
        log_row = LLMUsageLog()
        log_row.workshop_id = workshop_id
        log_row.service_used = "bedrock"
        log_row.model_used = Config.BEDROCK_MODEL_ID
        log_row.prompt_input_size = prompt_chars
        log_row.response_size = response_chars
        log_row.latency_ms = latency_ms
        db.session.add(log_row)
        db.session.flush()
    except Exception:  # pragma: no cover - logging must never break pipeline
        current_app.logger.exception("Failed to record agenda pipeline usage log")


# --- Simple agenda generator (mirrors other agents' pattern) -----------------
from langchain.prompts import PromptTemplate
from langchain.schema import BaseMessage
from app.utils.llm_bedrock import get_chat_llm
from app.utils.json_utils import extract_json_block

def run_agenda_pipeline(
    workshop: Workshop,
    *,
    agenda_draft: str,
    auto_generate: bool = True,
    llm_client=None,
) -> AgendaPipelineResult:
    """
    Simpler version of the agenda pipeline:
    - Builds one prompt with a compact schema
    - Uses PromptTemplate -> (prompt | llm).invoke(inputs)
    - Parses the first JSON block found
    - Validates with AgendaPayloadModel and persists
    """

    if workshop.id is None:
        raise AgendaGenerationError("Workshop must be flushed before agenda generation.")

    if not auto_generate:
        return _persist_manual_agenda(workshop, agenda_draft)

    # --- Context (kept minimal but compatible with the bundle) ---------------
    context_bundle = aggregate_pre_workshop_data(workshop.id, max_chars=CONTEXT_CHAR_LIMIT)
    context_blob = ""
    related_documents: List[Dict[str, Any]] = []
    draft_documents: List[Dict[str, Any]] = []
    context_metadata: Optional[Dict[str, Any]] = None
    document_section = "(no linked documents)"

    if context_bundle:
        context_blob = (context_bundle.json_for_prompt().strip()
                        or context_bundle.markdown_for_prompt().strip())
        document_section = _summarize_documents_for_prompt(context_bundle.payload.documents)
        context_metadata = _build_context_metadata(context_bundle)
        draft_docs_source = getattr(context_bundle.payload, "draft_documents", None) or []
        draft_documents = [_context_doc_to_dict(doc) for doc in draft_docs_source]

    # --- Inputs ----------------------------------------------------------------
    workshop_title = workshop.title or "Untitled Workshop"
    objective = workshop.objective or "(not specified)"
    duration_minutes = workshop.duration or 90
    scheduled_iso = workshop.date_time.isoformat(timespec="seconds") if getattr(workshop, "date_time", None) else ""
    agenda_draft_clean = (agenda_draft or "").strip() or "(no organizer draft provided)"
    existing_structured = workshop.agenda_json or ""

    # --- Compact schema (kept small, matches AgendaPayloadModel) --------------
    allowed = sorted(ALLOWED_TASK_TYPES)
    schema_snippet = """{
      "type": "object",
      "required": ["workshop_title","planned_duration_minutes","agenda","guidelines","icebreaker","tip","facilitator_tips","executive_summary"],
      "properties": {
        "workshop_title": {"type":"string"},
        "objective": {"type":"string"},
        "scheduled_start_iso": {"type":"string"},
        "planned_duration_minutes": {"type":"integer"},
        "agenda": {
          "type":"array",
          "minItems": 1,
          "items": {
            "type":"object",
            "required": ["position","title","description","task_type","duration_minutes"],
            "properties": {
              "position": {"type":"integer"},
              "title": {"type":"string"},
              "description": {"type":"string"},
              "task_type": {"type":"string"},
              "duration_minutes": {"type":"integer"},
              "start_offset_minutes": {"type":"integer"},
              "end_offset_minutes": {"type":"integer"},
              "notes": {"type":"string"},
              "origin": {"type":"string"}
            }
          }
        },
        "guidelines": {"type":"array","items":{"type":"string"}},
        "icebreaker": {"type":"string"},
        "facilitator_tips": {"type":"string"},
        "tip": {"type":"string"},
        "executive_summary": {"type":"string"},
        "confidence_level": {"type":"string"}
      }
    }"""

    # --- PromptTemplate (single prompt, no multi-message plumbing) ------------
    template = """
You are BrainStormX's Agenda Architect.
Return ONLY a single JSON object with NO markdown fences and NO commentary.

Hard rules:
- task_type MUST be one of: {allowed_task_types}
- Durations are integers (minutes) and should roughly sum to the planned duration.
- Items must be chronological, starting at minute 0. If offsets are missing, compute them.
- Improve vague titles into clear, outcome-oriented names.
- Populate guidelines, facilitator_tips, and executive_summary based on the agenda.

JSON Schema (follow exactly): {schema}

### Workshop Facts
Title: {title}
Objective: {objective}
Scheduled Start (ISO 8601): {scheduled_iso}
Planned Duration (minutes): {planned_duration}

### Organizer Agenda Draft
{agenda_draft}

### Prior Structured Agenda (if any)
{existing_structured}

### Linked Documents (top excerpts)
{document_section}

### Workspace Context
{context_blob}
""".strip()

    llm = llm_client or get_chat_llm(
        model_kwargs={
            "temperature": 0.25,
            "max_tokens": 3200,
            "top_p": 0.9,
        }
    )

    prompt = PromptTemplate.from_template(template)

    inputs = {
        "allowed_task_types": allowed,
        "schema": schema_snippet,
        "title": workshop_title,
        "objective": objective,
        "scheduled_iso": scheduled_iso,
        "planned_duration": duration_minutes,
        "agenda_draft": agenda_draft_clean,
        "existing_structured": existing_structured or "(none)",
        "document_section": document_section,
        "context_blob": context_blob or "(context unavailable)",
    }

    # --- Invoke LLM (simple) ---------------------------------------------------
    start_ts = time.perf_counter()
    if isinstance(llm, Runnable):
        chain = prompt | llm
        raw = chain.invoke(inputs)
    else:
        prompt_value = prompt.invoke(inputs)
        if hasattr(llm, "invoke"):
            raw = llm.invoke(prompt_value)
        elif callable(llm):
            raw = llm(prompt_value)
        else:
            raise TypeError(
                "LLM client must be a LangChain Runnable, callable, or expose invoke()"
            )
    print(f"\n\n\n\n\n\n[Agenda Pipeline] LLM raw response: {raw}\n\n\n\n\n\n")
    
    
    
    latency_ms = int((time.perf_counter() - start_ts) * 1000)

    text = _normalize_llm_response(raw)
    json_block = extract_json_block(text) or text.strip()

    # --- Parse & validate ------------------------------------------------------
    try:
        prepared_payload_json = _prepare_payload_json(json_block, workshop)
        payload = AgendaPayloadModel.model_validate_json(prepared_payload_json)
    except ValidationError as exc:
        current_app.logger.warning("Agenda simple pipeline validation error: %s", exc)
        raise AgendaGenerationError(
            "Generated agenda failed validation.",
            details=exc.errors(),
            code="validation_failed",
        ) from exc
    except Exception as exc:
        raise AgendaGenerationError("The language model response did not contain valid JSON.") from exc

    # --- Normalize, canonicalize, persist -------------------------------------
    normalized_items = _normalize_items(payload)
    canonical_payload = _build_canonical_payload(
        workshop,
        payload,
        normalized_items,
        related_documents=_collect_related_documents(workshop, context_bundle),
        draft_documents=draft_documents,
        context_snapshot=context_metadata,
    )
    agenda_json_str = json.dumps(canonical_payload, ensure_ascii=False, indent=2)

    _persist_to_database(
        workshop,
        agenda_json_str,
        normalized_items,
        payload.guidelines,
        payload.icebreaker,
        payload.facilitator_tips,
        payload.executive_summary,
        payload.confidence_level,
        agenda_draft,
    )

    _record_usage_log(
        workshop_id=workshop.id,
        prompt_chars=len(template) + sum(len(str(v)) for v in inputs.values()),
        response_chars=len(text),
        latency_ms=latency_ms,
    )

    return AgendaPipelineResult(
        agenda_json=agenda_json_str,
        items=normalized_items,
        guidelines=payload.guidelines,
        icebreaker=payload.icebreaker,
        facilitator_tips=payload.facilitator_tips,
        executive_summary=payload.executive_summary,
        confidence_level=payload.confidence_level,
        related_documents=_collect_related_documents(workshop, context_bundle),
        draft_documents=draft_documents,
        context_snapshot=context_metadata,
    )
