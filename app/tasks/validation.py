# app/tasks/validation.py
"""Lightweight JSON validation helpers for immediate cleanup.
We avoid external deps; perform minimal shape checks per task type.
"""
from typing import Dict, Any, List

REQUIRED_KEYS: Dict[str, List[str]] = {
    "warm-up": ["title", "task_description", "instructions", "task_duration"],
    "brainstorming": ["title", "task_description", "instructions", "task_duration"],
    "clustering_voting": ["title", "task_description", "instructions", "task_duration", "clusters"],
    "results_feasibility": [
        "title",
        "task_description",
        "instructions",
        "task_duration",
        "analysis",
        "document_spec",
    ],
    # Results phases that render generated artifacts and narration
    "results_prioritization": [
        "title",
        "task_description",
        "instructions",
        "task_duration",
        "prioritized",
        "document_spec",
    ],
    "results_action_plan": ["title", "task_description", "instructions", "task_duration", "action_items"],
    "discussion": ["title", "task_description", "instructions", "task_duration"],
    "summary": ["title", "task_description", "instructions", "task_duration", "summary_report"],
    # --- New generic tasks ---
    "meeting": ["title", "task_description", "instructions", "task_duration"],
    # Keep presenter/document soft-optional in validation; UI can prompt when missing
    "presentation": ["title", "task_description", "instructions", "task_duration"],
    # speaker is optional at payload time; facilitator can assign live
    "speech": ["title", "task_description", "instructions", "task_duration"],
    "framing": ["title", "task_description", "instructions", "task_duration", "tts_script"],
    # Generic vote requires items list in payload
    "vote_generic": ["title", "task_description", "instructions", "task_duration", "items"],
}

def validate_payload(task_type: str, payload: Dict[str, Any]) -> bool:
    keys = REQUIRED_KEYS.get(task_type, [])
    for k in keys:
        if k not in payload:
            return False
    # Additional minimal checks
    dur = payload.get("task_duration")
    try:
        if dur is None:
            return False
        int(dur)
    except Exception:
        return False
    if task_type == "clustering_voting" and not isinstance(payload.get("clusters"), list):
        return False
    if task_type == "results_feasibility":
        analysis = payload.get("analysis")
        doc_spec = payload.get("document_spec")
        if not isinstance(analysis, dict) or not isinstance(doc_spec, dict):
            return False
        clusters = analysis.get("clusters") if isinstance(analysis, dict) else None
        if clusters is None or not isinstance(clusters, list):
            return False
    if task_type == "results_prioritization":
        prioritized = payload.get("prioritized")
        if not isinstance(prioritized, list) or not prioritized:
            return False
        doc_spec = payload.get("document_spec")
        if not isinstance(doc_spec, dict):
            return False
    return True
