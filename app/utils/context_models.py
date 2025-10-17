from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

CONTEXT_VERSION = "1.0"
DEFAULT_MAX_CONTEXT_CHARS = 6000


class ContextUserRef(BaseModel):
    user_id: int
    name: str
    email: Optional[str] = None


class WorkshopMetadata(BaseModel):
    id: int
    title: str
    objective: Optional[str] = None
    status: str
    workspace_id: Optional[int] = None
    scheduled_start_iso: Optional[str] = None
    duration_minutes: Optional[int] = None
    agenda_auto_generate: bool = True
    agenda_generated_source: Optional[str] = None
    agenda_generated_at_iso: Optional[str] = None
    agenda_confidence: Optional[str] = None
    created_by: Optional[ContextUserRef] = None


class WorkspaceSummary(BaseModel):
    id: int
    name: str
    description: Optional[str] = None


class AgendaItemContext(BaseModel):
    position: int
    title: str
    description: Optional[str] = None
    task_type: Optional[str] = None
    duration_minutes: Optional[int] = None
    start_offset_minutes: Optional[int] = None
    end_offset_minutes: Optional[int] = None
    origin: Optional[str] = None


class AgendaContext(BaseModel):
    draft_text: Optional[str] = None
    items: List[AgendaItemContext] = Field(default_factory=list)
    total_duration_minutes: Optional[int] = None


class GeneratedArtifacts(BaseModel):
    rules: Optional[str] = None
    icebreaker: Optional[str] = None
    tip: Optional[str] = None
    facilitator_guidelines: List[str] = Field(default_factory=list)
    facilitator_tips: List[str] = Field(default_factory=list)
    facilitator_summary: Optional[str] = None
    agenda_confidence: Optional[str] = None


class ContextParticipant(BaseModel):
    user_id: int
    name: str
    email: Optional[str] = None
    role: str
    status: str
    job_title: Optional[str] = None
    organization: Optional[str] = None


class ContextDocument(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    summary: Optional[str] = None
    excerpt: Optional[str] = None
    source: str = Field(default="linked")
    highlights: List[str] = Field(default_factory=list)


class WorkspaceContextPayload(BaseModel):
    version: str = Field(default=CONTEXT_VERSION)
    collected_at_iso: str = Field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))
    workshop: WorkshopMetadata
    workspace: Optional[WorkspaceSummary] = None
    participants: List[ContextParticipant] = Field(default_factory=list)
    agenda: AgendaContext = Field(default_factory=AgendaContext)
    generated: GeneratedArtifacts = Field(default_factory=GeneratedArtifacts)
    documents: List[ContextDocument] = Field(default_factory=list)
    draft_documents: List[ContextDocument] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


@dataclass
class WorkspaceContextBundle:
    payload: WorkspaceContextPayload
    json_text: str
    markdown_fallback: str
    max_chars: int = DEFAULT_MAX_CONTEXT_CHARS

    def json_for_prompt(self) -> str:
        """Return the JSON context trimmed within the max char budget."""
        if len(self.json_text) <= self.max_chars:
            return self.json_text
        return self.json_text[: self.max_chars]

    def markdown_for_prompt(self) -> str:
        """Return the markdown fallback trimmed within the max char budget."""
        if len(self.markdown_fallback) <= self.max_chars:
            return self.markdown_fallback
        return self.markdown_fallback[: self.max_chars]

    def _text_view(self) -> str:
        """Produce the preferred string representation for legacy integrations."""
        return self.json_for_prompt()

    def __str__(self) -> str:  # pragma: no cover - convenience for logging
        return self._text_view()

    def __repr__(self) -> str:  # pragma: no cover - convenience for logging
        return f"WorkspaceContextBundle(len={len(self._text_view())})"

    def __len__(self) -> int:
        return len(self._text_view())

    def __getitem__(self, item):
        return self._text_view().__getitem__(item)

    def __iter__(self):
        return iter(self._text_view())

    def __getattr__(self, name):
        # Delegate common string helpers (strip, split, etc.) when callers
        # mistakenly treat the bundle as a string.
        text = self._text_view()
        try:
            return getattr(text, name)
        except AttributeError as exc:  # pragma: no cover - defensive
            raise exc


def truncate_text(value: Optional[str], *, max_length: int) -> Optional[str]:
    if not value:
        return value
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"
