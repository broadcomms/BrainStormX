# app/tasks/registry.py
"""Minimal task registry for immediate cleanup phase.
Defines metadata for known task types: event names and default durations.
This is intentionally small; we can extend with schemas and generators later.
"""
from typing import Dict, List, TypedDict

class TaskMeta(TypedDict, total=False):
    id: str
    event: str
    default_duration: int
    inputs: List[str]
    outputs: List[str]

_WARM_META: TaskMeta = {
    "id": "warm-up",
    "event": "warm_up_start",
    "default_duration": 180,
    "inputs": [],  # Warm-up does not produce structured artifacts needed by later phases
    "outputs": [],
}

TASK_REGISTRY: Dict[str, TaskMeta] = {
    "warm-up": _WARM_META,
    "warm_up": _WARM_META,
    "brainstorming": {
        "id": "brainstorming",
        "event": "task_ready",
        "default_duration": 60,
        # Produces a pool of ideas used by clustering
        "inputs": [],
        "outputs": ["ideas"],
    },
    "clustering_voting": {
        "id": "clustering_voting",
        "event": "clusters_ready",
        "default_duration": 60,
        # Requires ideas from brainstorming, produces clusters (and votes)
        "inputs": ["ideas"],
        "outputs": ["clusters", "votes"],
    },
    "results_feasibility": {
        "id": "results_feasibility",
        "event": "feasibility_ready",
        "default_duration": 60,
        # Requires clusters (with optional votes), produces feasibility insights
        "inputs": ["clusters"],
        "outputs": ["feasibility"],
    },
    # New: Prioritization & Shortlisting results phase
    "results_prioritization": {
        "id": "results_prioritization",
        "event": "prioritization_ready",
        "default_duration": 900,
        # Requires clusters and votes (soft), uses feasibility hints/objective context if present
        "inputs": ["clusters", "votes", "feasibility?"],
        "outputs": ["shortlist", "prioritized_pdf"],
    },
    # New: Action Plan results phase
    "results_action_plan": {
        "id": "results_action_plan",
        "event": "action_plan_ready",
        "default_duration": 900,
        # Requires shortlist-like inputs
        "inputs": ["shortlist"],
        "outputs": ["action_items", "action_plan_pdf"],
    },
    "discussion": {
        "id": "discussion",
        "event": "discussion_ready",
        "default_duration": 60,
        # Free-form discussion can occur anywhere; consider notes as output
        "inputs": [],
        "outputs": ["notes"],
    },
    "summary": {
        "id": "summary",
        "event": "summary_ready",
        "default_duration": 60,
        # Summary synthesizes across everything; no strict inputs required
        "inputs": [],
        "outputs": ["summary_report"],
    },
    # --- New generic session/task types ---
    "meeting": {
        "id": "meeting",
        "event": "meeting_ready",
        "default_duration": 3600,
        "inputs": [],
        "outputs": [],
    },
    "presentation": {
        "id": "presentation",
        "event": "presentation_ready",
        "default_duration": 900,
        # Optional dependency on documents linked to the workshop
        "inputs": ["documents?"],
        "outputs": [],
    },
    "framing": {
        "id": "framing",
        "event": "framing_ready",
        "default_duration": 600,
        "inputs": [],
        "outputs": [],
    },
    "speech": {
        "id": "speech",
        "event": "speech_ready",
        "default_duration": 600,
        "inputs": [],
        "outputs": [],
    },
    # Standard, generic voting that can target clusters or ideas (two-stage support)
    "vote_generic": {
        "id": "vote_generic",
        "event": "vote_ready",
        "default_duration": 600,
        # Soft inputs: can work with clusters (from clustering_voting) or ideas (from brainstorming)
        "inputs": ["clusters?", "ideas?"],
        "outputs": ["votes"],
    },
}
