from __future__ import annotations

from typing import Dict

from pydantic import BaseModel, conint

from app.assistant.tooling import ToolSpec
from app.assistant.skills import (
    skill_add_action_item,
    skill_capture_decision,
    skill_explain_chart,
    skill_fetch_phase_snapshot,
    skill_fetch_workshop_context,
    skill_generate_devil_advocate,
    skill_list_decisions,
    skill_retrieve_workshop_phase,
    skill_render_whiteboard_snapshot,
    skill_search_documents,
    skill_summarize_transcripts,
)


class SummarizeTranscriptsArgs(BaseModel):
    window_minutes: conint(ge=1, le=120) = 10


class DevilAdvocateArgs(BaseModel):
    cluster_id: int


class CaptureDecisionArgs(BaseModel):
    topic: str
    decision: str
    rationale: str | None = None
    owner_user_id: int | None = None


class ActionItemArgs(BaseModel):
    title: str
    owner_user_id: int | None = None
    due_date: str | None = None
    metric: str | None = None


class ExplainChartArgs(BaseModel):
    chart_id: str


class WhiteboardSnapshotArgs(BaseModel):
    format: str | None = "image"


class SearchDocumentsArgs(BaseModel):
    query: str


TOOL_REGISTRY: Dict[str, ToolSpec] = {
    "fetch_phase_snapshot": ToolSpec(
        name="fetch_phase_snapshot",
        fn=skill_fetch_phase_snapshot,
        docs="Return cached payload snapshots for each workshop phase.",
        allow_guest=False,
    ),
    "fetch_workshop_context": ToolSpec(
        name="fetch_workshop_context",
        fn=skill_fetch_workshop_context,
        docs="Return high level workshop metadata and participants.",
        allow_guest=True,
    ),
    "retrieve_workshop_phase": ToolSpec(
        name="retrieve_workshop_phase",
        fn=skill_retrieve_workshop_phase,
        docs="Return current workshop phase, task metadata, and timer snapshot.",
        allow_guest=False,
    ),
    "search_documents": ToolSpec(
        name="search_documents",
        fn=skill_search_documents,
        args_schema=SearchDocumentsArgs,
        docs="Lookup linked documents by fuzzy title/description match.",
    ),
    "list_decisions": ToolSpec(
        name="list_decisions",
        fn=skill_list_decisions,
        docs="Return recently captured decisions for this workshop.",
    ),
    "add_action_item": ToolSpec(
        name="add_action_item",
        fn=skill_add_action_item,
        args_schema=ActionItemArgs,
        roles_allowed={"organizer", "facilitator"},
        docs="Persist a new action item with optional owner, due date, and metric.",
    ),
    "summarize_transcripts": ToolSpec(
        name="summarize_transcripts",
        fn=skill_summarize_transcripts,
        args_schema=SummarizeTranscriptsArgs,
        docs="Summarize recent transcript notes over the provided time window.",
    ),
    "generate_devil_advocate": ToolSpec(
        name="generate_devil_advocate",
        fn=skill_generate_devil_advocate,
        args_schema=DevilAdvocateArgs,
        docs="Derive devil's advocate challenges for a cluster.",
        roles_allowed={"organizer", "facilitator"},
    ),
    "capture_decision": ToolSpec(
        name="capture_decision",
        fn=skill_capture_decision,
        args_schema=CaptureDecisionArgs,
        docs="Save a decision outcome with rationale and optional owner.",
        roles_allowed={"organizer", "facilitator"},
    ),
    "render_whiteboard_snapshot": ToolSpec(
        name="render_whiteboard_snapshot",
        fn=skill_render_whiteboard_snapshot,
        args_schema=WhiteboardSnapshotArgs,
        docs="Return current whiteboard state for grounding the response.",
    ),
    "explain_chart": ToolSpec(
        name="explain_chart",
        fn=skill_explain_chart,
        args_schema=ExplainChartArgs,
        docs="Provide metadata about a chart or visualization for LLM narration.",
    ),
}


# Maintain backwards compatibility for legacy imports
SKILLS = {name: spec.fn for name, spec in TOOL_REGISTRY.items()}
