# app/service/discussion_prompt.py
"""Prompt contracts and templates for the discussion orchestration pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal

from langchain_core.prompts import PromptTemplate

Mode = Literal["initial", "devil_advocate", "mediator", "scribe"]


@dataclass(frozen=True)
class ModeContract:
    """Defines the instruction and JSON schema directives for a discussion mode."""

    name: Mode
    instruction: str
    schema_directive: str


_MODE_CONTRACTS: Dict[Mode, ModeContract] = {
    "initial": ModeContract(
        name="initial",
        instruction=(
            "Generate the kickoff payload for the discussion. Provide narration, mediator prompt, "
            "scribe summary, devil-advocate prompts, and seed decisions plus notes."
        ),
        schema_directive=(
            "- discussion_notes: array[{ts, speaker_user_id|null, point}].\n"
            "- decisions: array[{topic, decision, owner_user_id|null, rationale, cluster_id|null}].\n"
            "- devil_advocate: array[{cluster_id|null, cluster_title, counterargument, probing_question}].\n"
            "- mediator_prompt: string.\n"
            "- scribe_summary: string.\n"
            "- narration: string.\n"
            "- tts_script: string (90-160 words).\n"
            "- tts_read_time_seconds: integer >= 45."
        ),
    ),
    "devil_advocate": ModeContract(
        name="devil_advocate",
        instruction=(
            "Interrogate the current shortlist or focus cluster. Produce sharp counterarguments and "
            "probing questions that expose hidden risks or missing evidence."
        ),
        schema_directive=(
            "- devil_advocate: array[{cluster_id|null, cluster_title, counterargument, probing_question}] (required).\n"
            "- discussion_notes: optional array[{ts, speaker_user_id|null, point}].\n"
            "- scribe_summary: optional string."
        ),
    ),
    "mediator": ModeContract(
        name="mediator",
        instruction=(
            "Synthesize key points into actionable decisions. Clarify ownership and rationale so the "
            "group can vote or confirm."
        ),
        schema_directive=(
            "- decisions: array[{topic, decision, owner_user_id|null, rationale, cluster_id|null}] (required).\n"
            "- mediator_prompt: string (required).\n"
            "- scribe_summary: optional string."
        ),
    ),
    "scribe": ModeContract(
        name="scribe",
        instruction=(
            "Capture what was just discussed. Produce structured notes and a crisp summary that can be "
            "posted to the forum archive."
        ),
        schema_directive=(
            "- discussion_notes: array[{ts, speaker_user_id|null, point}] (required).\n"
            "- scribe_summary: string (required)."
        ),
    ),
}


_DISCUSSION_TEMPLATE = """
You are part of an AI facilitation triad inside a live workshop. You are operating in MODE: {mode}.
{mode_instruction}

Guidelines:
- Use only the supplied context.
- Preserve IDs as provided (cluster_id, idea_id, owner_user_id). If unknown, use null.
- When a list has no content, return an empty array, not null.
- Do not fabricate data; if context is missing, state that in the rationale or summary.

Return STRICT JSON with keys:
{schema_block}

Context:
Workshop Overview:
{workshop_overview}

Problem & Criteria:
{framing_core}

Prioritization Shortlist:
{prioritized_json}

Feasibility Insights:
{feasibility_json}

Clusters:
{clusters_json}

Recent Chat:
{chat_json}

Recent Transcripts:
{transcripts_json}

Existing Discussion Notes:
{prior_notes_json}

Existing Decisions:
{prior_decisions_json}

Forum Snapshot:
{forum_snapshot_json}

Forum Deep Dive:
{forum_detailed_json}

Cadence Settings:
{cadence_settings_json}

Mode Payload:
{mode_payload_json}

Phase Context:
{phase_context}

Feasibility Risk Annex:
{feasibility_annex_json}

Framing Risk Checklist:
{framing_risk_checklist_json}

Action Items:
{action_items_json}
""".strip()


def get_mode_contract(mode: Mode) -> ModeContract:
    """Return the prompt contract for a given discussion mode."""
    if mode not in _MODE_CONTRACTS:
        raise KeyError(f"Unsupported discussion mode: {mode}")
    return _MODE_CONTRACTS[mode]


def build_prompt_template() -> PromptTemplate:
    """Return the compiled prompt template shared across discussion modes."""
    return PromptTemplate.from_template(_DISCUSSION_TEMPLATE)


__all__ = ["Mode", "ModeContract", "get_mode_contract", "build_prompt_template"]
