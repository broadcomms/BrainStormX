from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator


class PersonaType(str, Enum):
    GUIDE = "guide"
    SCRIBE = "scribe"
    MEDIATOR = "mediator"
    DEVIL = "devil"
    ANALYST = "analyst"


class AssistantQuery(BaseModel):
    workshop_id: int
    user_id: Optional[int] = None
    thread_id: Optional[int] = None
    text: str
    persona_hint: Optional[PersonaType] = Field(
        default=None,
        alias="persona",
        validation_alias=AliasChoices("persona", "persona_hint"),
    )
    mode: Optional[str] = None

    @field_validator("text")
    @classmethod
    def _ensure_text(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("text cannot be empty")
        return cleaned


class AssistantToolCall(BaseModel):
    name: str
    args: Dict[str, Any] = Field(default_factory=dict)


class AssistantCitationPayload(BaseModel):
    source_type: Optional[str] = None
    source_ref: Optional[str] = None
    display_label: Optional[str] = None
    document_id: Optional[int] = None
    snippet_hash: Optional[str] = None
    start_char: Optional[int] = None
    end_char: Optional[int] = None


class AssistantReply(BaseModel):
    role: str = "assistant"
    persona: PersonaType = PersonaType.GUIDE
    text: str
    speech: Optional[Dict[str, Any]] = None
    citations: List[AssistantCitationPayload] = Field(default_factory=list)
    tool_calls: List[AssistantToolCall] = Field(default_factory=list)
    proposed_actions: List[Dict[str, Any]] = Field(default_factory=list)
    ui_hints: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _coerce_tool_calls(cls, value: Any) -> List[Dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, dict):
            value = [value]
        elif not isinstance(value, list):
            value = [value]
        normalized: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("tool") or item.get("action")
            if isinstance(name, str):
                name = name.strip()
            args = item.get("args") or item.get("arguments") or {}
            if args is None:
                args = {}
            if not isinstance(args, dict):
                continue
            if not name:
                continue
            normalized.append({"name": name, "args": args})
        return normalized

    @field_validator("persona", mode="before")
    @classmethod
    def _normalize_persona(cls, value: Any) -> PersonaType:
        if isinstance(value, PersonaType):
            return value
        if value is None:
            return PersonaType.GUIDE
        normalized = str(value).strip().lower()
        alias_map = {
            "workshop guide": PersonaType.GUIDE.value,
            "assistant guide": PersonaType.GUIDE.value,
            "guide persona": PersonaType.GUIDE.value,
            "scribe persona": PersonaType.SCRIBE.value,
            "mediator persona": PersonaType.MEDIATOR.value,
            "devils advocate": PersonaType.DEVIL.value,
            "devil persona": PersonaType.DEVIL.value,
            "analyst persona": PersonaType.ANALYST.value,
        }
        normalized = alias_map.get(normalized, normalized)
        try:
            return PersonaType(normalized)
        except ValueError:
            return PersonaType.GUIDE

    @field_validator("proposed_actions", mode="before")
    @classmethod
    def _coerce_proposed_actions(cls, value: Any) -> List[Dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, dict):
            value = [value]
        elif not isinstance(value, list):
            value = [value]
        coerced: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                coerced.append(item)
            elif isinstance(item, str):
                coerced.append({"type": "note", "text": item})
        return coerced

    @field_validator("citations", mode="before")
    @classmethod
    def _coerce_citations(cls, value: Any) -> List[Dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, str):
            return [{"source_ref": value, "display_label": value}]
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            normalized: List[Dict[str, Any]] = []
            for item in value:
                if isinstance(item, dict):
                    payload = dict(item)
                    doc_id = payload.get("document_id")
                    if doc_id is not None:
                        try:
                            payload["document_id"] = int(doc_id)
                        except (TypeError, ValueError):
                            if payload.get("display_label") is None and doc_id:
                                payload["display_label"] = str(doc_id)
                            payload["document_id"] = None
                    normalized.append(payload)
                elif isinstance(item, str):
                    normalized.append({"source_ref": item, "display_label": item})
            return normalized
        return []


class AssistantTurnLog(BaseModel):
    thread_id: int
    persona: PersonaType
    latency_ms: Optional[int] = None
    token_usage: Optional[int] = None
    tool_count: int = 0
    plan_json: Optional[Dict[str, Any]] = None
    composed_json: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ToolExecutionResult(BaseModel):
    name: str
    success: bool
    output: Any = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None


class AssistantFeedbackPayload(BaseModel):
    turn_id: int
    rating: Literal['up', 'down', 'flag']
    comment: Optional[str] = None
