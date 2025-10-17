"""Structured aggregations for LLM context."""

from __future__ import annotations

import json
import logging
from typing import Iterable, List, Optional

from flask import current_app, has_app_context
from app.models import Document, Workshop, WorkshopAgenda, WorkshopDocument, WorkshopParticipant
from app.utils.context_models import (
    AgendaContext,
    AgendaItemContext,
    ContextDocument,
    ContextParticipant,
    ContextUserRef,
    DEFAULT_MAX_CONTEXT_CHARS,
    GeneratedArtifacts,
    WorkspaceContextBundle,
    WorkspaceContextPayload,
    WorkshopMetadata,
    WorkspaceSummary,
    truncate_text,
)


def aggregate_pre_workshop_data(
    workshop_id: int, *, max_chars: int = DEFAULT_MAX_CONTEXT_CHARS
) -> Optional[WorkspaceContextBundle]:
    """Build a structured snapshot of the workshop for downstream prompts."""

    if has_app_context():
        log = current_app.logger
    else:
        log = logging.getLogger(__name__)

    log.debug("Aggregating workshop context", extra={"workshop_id": workshop_id})

    workshop = Workshop.query.filter_by(id=workshop_id).first()

    if not workshop:
        log.warning("Workshop not found for aggregation", extra={"workshop_id": workshop_id})
        return None

    participants = _load_participants(workshop)
    documents = _load_documents(workshop)

    payload = WorkspaceContextPayload(
        workshop=_build_workshop_metadata(workshop),
        workspace=_build_workspace_summary(workshop),
        participants=participants,
        agenda=_build_agenda_context(workshop),
        generated=_build_generated_artifacts(workshop),
        documents=documents,
    )

    payload_dict = payload.model_dump(exclude_none=True)
    json_text = json.dumps(payload_dict, separators=(",", ":"), ensure_ascii=False)
    markdown_fallback = _build_markdown_fallback(payload)

    bundle = WorkspaceContextBundle(
        payload=payload,
        json_text=json_text,
        markdown_fallback=markdown_fallback,
        max_chars=max_chars,
    )

    log.debug(
        "Aggregated workshop context sizes",
        extra={
            "workshop_id": workshop_id,
            "json_length": len(json_text),
            "markdown_length": len(markdown_fallback),
        },
    )

    return bundle


def get_pre_workshop_context_json(
    workshop_id: int, *, max_chars: int = DEFAULT_MAX_CONTEXT_CHARS
) -> str:
    """Retrieve the serialized workshop context clipped for LLM prompts."""

    bundle = aggregate_pre_workshop_data(workshop_id, max_chars=max_chars)
    if not bundle:
        return ""
    return bundle.json_for_prompt()


def get_pre_workshop_context_markdown(
    workshop_id: int, *, max_chars: int = DEFAULT_MAX_CONTEXT_CHARS
) -> str:
    """Return the markdown fallback representation of the workshop context."""

    bundle = aggregate_pre_workshop_data(workshop_id, max_chars=max_chars)
    if not bundle:
        return ""
    return bundle.markdown_for_prompt()


def _load_participants(workshop: Workshop) -> List[ContextParticipant]:
    records: Iterable[WorkshopParticipant] = (
        WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()
    )

    participants: List[ContextParticipant] = []
    for participant in records:
        user = participant.user
        if user is None:
            continue
        participants.append(
            ContextParticipant(
                user_id=user.user_id,
                name=user.display_name,
                email=user.email,
                role=participant.role,
                status=participant.status,
                job_title=user.job_title,
                organization=user.organization,
            )
        )

    participants.sort(key=lambda p: (p.role != "organizer", p.name.lower()))
    return participants


def _load_documents(workshop: Workshop) -> List[ContextDocument]:
    links: Iterable[WorkshopDocument] = (
        WorkshopDocument.query.filter_by(workshop_id=workshop.id).all()
    )

    documents: List[ContextDocument] = []
    for link in links:
        doc: Optional[Document] = getattr(link, "document", None)
        if not doc:
            continue
        highlights = _collect_document_highlights(doc)
        documents.append(
            ContextDocument(
                id=doc.id,
                title=doc.title,
                description=truncate_text(doc.description, max_length=240),
                summary=truncate_text(doc.summary, max_length=600),
                excerpt=_extract_document_excerpt(doc),
                source="linked",
                highlights=highlights,
            )
        )

    return documents


def _build_workshop_metadata(workshop: Workshop) -> WorkshopMetadata:
    creator = workshop.creator
    created_by = (
        ContextUserRef(
            user_id=creator.user_id,
            name=creator.display_name,
            email=creator.email,
        )
        if creator
        else None
    )

    agenda_generated_at_iso = (
        workshop.agenda_generated_at.isoformat(timespec="seconds")
        if getattr(workshop, "agenda_generated_at", None)
        else None
    )

    return WorkshopMetadata(
        id=workshop.id,
        title=workshop.title,
        objective=truncate_text(workshop.objective, max_length=600),
        status=workshop.status,
        workspace_id=workshop.workspace_id,
        scheduled_start_iso=workshop.date_time.isoformat()
        if workshop.date_time
        else None,
        duration_minutes=workshop.duration,
        agenda_auto_generate=getattr(workshop, "agenda_auto_generate", True),
        agenda_generated_source=getattr(workshop, "agenda_generated_source", None),
        agenda_generated_at_iso=agenda_generated_at_iso,
        agenda_confidence=getattr(workshop, "agenda_confidence", None),
        created_by=created_by,
    )


def _build_workspace_summary(workshop: Workshop) -> Optional[WorkspaceSummary]:
    workspace = workshop.workspace
    if not workspace:
        return None
    return WorkspaceSummary(
        id=workspace.workspace_id,
        name=workspace.name,
        description=truncate_text(workspace.description, max_length=400),
    )


def _build_agenda_context(workshop: Workshop) -> AgendaContext:
    raw_draft = getattr(workshop, "agenda_draft_plaintext", None) or workshop.agenda
    draft_text = truncate_text(raw_draft, max_length=800)
    items: List[AgendaItemContext] = []
    total_duration = 0

    for item in getattr(workshop, "agenda_items", []) or []:
        if not isinstance(item, WorkshopAgenda):
            continue
        duration = (item.duration_minutes or item.estimated_duration or 0) or 0
        total_duration += duration
        items.append(
            AgendaItemContext(
                position=item.position,
                title=item.activity_title,
                description=truncate_text(item.activity_description, max_length=280),
                task_type=item.task_type,
                duration_minutes=duration or None,
                start_offset_minutes=_seconds_to_minutes(item.start_offset),
                end_offset_minutes=_seconds_to_minutes(item.end_offset),
                origin=item.origin,
            )
        )

    if not items and draft_text:
        try:
            parsed = json.loads(draft_text)
            if isinstance(parsed, list):
                for idx, node in enumerate(parsed, start=1):
                    title = node.get("title") or node.get("phase") or f"Item {idx}"
                    description = node.get("description") or node.get("summary")
                    duration_val = node.get("duration_minutes") or node.get("duration")
                    duration_minutes = None
                    if isinstance(duration_val, (int, float)):
                        duration_minutes = int(duration_val)
                        total_duration += duration_minutes
                    items.append(
                        AgendaItemContext(
                            position=idx,
                            title=title,
                            description=truncate_text(description, max_length=280),
                            task_type=node.get("task_type"),
                            duration_minutes=duration_minutes,
                            origin=node.get("origin"),
                        )
                    )
        except (TypeError, ValueError):
            pass

    return AgendaContext(
        draft_text=draft_text,
        items=items,
        total_duration_minutes=total_duration or None,
    )


def _seconds_to_minutes(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if value <= 0:
        return 0
    return int(value // 60)


def _build_generated_artifacts(workshop: Workshop) -> GeneratedArtifacts:
    def _split_lines(value: Optional[str]) -> List[str]:
        if not value:
            return []
        segments = [segment.strip() for segment in value.splitlines() if segment.strip()]
        return segments[:8]

    return GeneratedArtifacts(
        rules=truncate_text(workshop.rules, max_length=600),
        icebreaker=truncate_text(workshop.icebreaker, max_length=320),
        tip=truncate_text(workshop.tip, max_length=320),
        facilitator_guidelines=_split_lines(getattr(workshop, "facilitator_guidelines", "")),
        facilitator_tips=_split_lines(getattr(workshop, "facilitator_tips", "")),
        facilitator_summary=truncate_text(getattr(workshop, "facilitator_summary", None), max_length=600),
        agenda_confidence=getattr(workshop, "agenda_confidence", None),
    )


def _extract_document_excerpt(document: Document) -> Optional[str]:
    if document.summary:
        return truncate_text(document.summary, max_length=600)
    if document.content:
        return truncate_text(document.content, max_length=600)
    if document.markdown:
        return truncate_text(document.markdown, max_length=600)
    return None


def _collect_document_highlights(document: Document, *, max_items: int = 3) -> List[str]:
    highlights: List[str] = []

    # Prefer explicit highlight fields when available (summary split into bullets)
    if document.summary:
        for line in document.summary.splitlines():
            text = line.strip()
            if not text:
                continue
            highlights.append(truncate_text(text, max_length=280) or text)
            if len(highlights) >= max_items:
                return highlights[:max_items]

    # Fallback to chunked content stored during ingestion
    try:
        chunk_iterable = list(document.chunks)  # type: ignore[arg-type]
    except TypeError:
        chunk_iterable = []
    except Exception:
        chunk_iterable = []

    chunk_iterable.sort(key=lambda chunk: getattr(chunk, "id", 0))

    for chunk in chunk_iterable:
        text = getattr(chunk, "content", "")
        if not text:
            continue
        trimmed = truncate_text(text.strip(), max_length=280)
        if trimmed:
            highlights.append(trimmed)
            if len(highlights) >= max_items:
                break

    if not highlights and document.content:
        fallback = truncate_text(document.content.strip(), max_length=280)
        if fallback:
            highlights.append(fallback)

    return highlights[:max_items]


def _build_markdown_fallback(payload: WorkspaceContextPayload) -> str:
    lines: List[str] = []
    workshop = payload.workshop
    lines.append(f"Workshop: {workshop.title} (ID {workshop.id})")
    if workshop.objective:
        lines.append(f"Objective: {workshop.objective}")
    if workshop.scheduled_start_iso:
        lines.append(f"Scheduled: {workshop.scheduled_start_iso}")
    lines.append(f"Status: {workshop.status}")
    lines.append("")

    if payload.workspace:
        lines.append(f"Workspace: {payload.workspace.name}")
        if payload.workspace.description:
            lines.append(f"Description: {payload.workspace.description}")
        lines.append("")

    lines.append(f"Participants ({len(payload.participants)}):")
    for participant in payload.participants:
        role_label = participant.role.capitalize()
        details = f"- {participant.name} ({role_label}, {participant.status})"
        if participant.job_title:
            details += f" — {participant.job_title}"
        if participant.organization:
            details += f" @ {participant.organization}"
        lines.append(details)
    if not payload.participants:
        lines.append("- None registered")
    lines.append("")

    if payload.agenda.items:
        lines.append("Agenda Items:")
        for item in payload.agenda.items:
            duration = f" ({item.duration_minutes} min)" if item.duration_minutes else ""
            description = f": {item.description}" if item.description else ""
            lines.append(f"- {item.title}{duration}{description}")
        lines.append("")
    elif payload.agenda.draft_text:
        lines.append("Agenda Draft:")
        lines.append(payload.agenda.draft_text)
        lines.append("")

    if payload.generated.rules:
        lines.append("Rules:")
        lines.append(payload.generated.rules)
        lines.append("")
    if payload.generated.icebreaker:
        lines.append(f"Icebreaker: {payload.generated.icebreaker}")
    if payload.generated.tip:
        lines.append(f"Prep Tip: {payload.generated.tip}")
    if payload.generated.facilitator_summary:
        lines.append(f"Facilitator Summary: {payload.generated.facilitator_summary}")
    if payload.generated.facilitator_guidelines:
        lines.append("Facilitator Guidelines:")
        lines.extend(f"- {line}" for line in payload.generated.facilitator_guidelines)
    if payload.generated.facilitator_tips:
        lines.append("Facilitator Tips:")
        lines.extend(f"- {line}" for line in payload.generated.facilitator_tips)
    if payload.generated.agenda_confidence:
        lines.append(f"Agenda Confidence: {payload.generated.agenda_confidence}")
    if payload.generated.rules or payload.generated.icebreaker or payload.generated.tip:
        lines.append("")

    if payload.documents:
        lines.append("Linked Documents:")
        for doc in payload.documents:
            doc_line = f"- {doc.title}"
            if doc.summary:
                doc_line += f": {doc.summary}"
            elif doc.description:
                doc_line += f": {doc.description}"
            lines.append(doc_line)
        lines.append("")

    return "\n".join(lines).strip()

