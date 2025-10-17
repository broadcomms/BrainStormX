"""Phase context models for LLM injection.

This module defines data structures for providing rich, phase-aware context
to the Assistant LLM at every stage of the workshop lifecycle.

Data flow:
1. PhaseContextProvider builds PhaseContextBundle from workshop data
2. PhaseContextBundle contains previous/current/next phase contexts
3. PromptBuilder formats bundle for LLM consumption
4. LLM receives structured phase context in every query
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from enum import Enum


class PhaseStatus(Enum):
    """Phase completion status."""
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


@dataclass
class PhaseDocument:
    """Document generated or used in a specific workshop phase.
    
    Attributes:
        id: Document database ID
        title: Document title
        url: Public URL for document access (if available)
        phase: Phase that generated/uses this document (e.g., 'framing', 'results_feasibility')
        document_type: Type classification (e.g., 'framing_brief', 'feasibility_report')
        summary: Optional brief description of document contents
    """
    id: int
    title: str
    url: Optional[str]
    phase: str
    document_type: str
    summary: Optional[str] = None


@dataclass
class PreviousPhaseContext:
    """Summary of a completed phase for token-efficient LLM context.
    
    Provides narrative summary and key artifacts from completed phases
    without overwhelming the context window with full payload data.
    
    Attributes:
        phase_name: Internal phase name (e.g., 'framing', 'clustering_voting')
        phase_label: Human-friendly label (e.g., 'Briefing', 'Clustering')
        status: Completion status (typically COMPLETED)
        summary: 2-3 sentence narrative summary of what happened
        key_artifacts: Structured dict of important phase outputs
        documents: List of documents generated in this phase
        completed_at: ISO timestamp when phase ended
    """
    phase_name: str
    phase_label: str
    status: PhaseStatus
    
    # Core narrative summary (token-efficient)
    summary: str
    
    # Key artifacts (structured)
    key_artifacts: Dict[str, Any] = field(default_factory=dict)
    # Examples:
    # - framing: {"problem_statement": "...", "success_criteria": [...]}
    # - brainstorming: {"idea_count": 25, "ai_seed_ideas": 3}
    # - clustering_voting: {"cluster_count": 5, "top_cluster": "Policy Frameworks"}
    
    # Documents generated
    documents: List[PhaseDocument] = field(default_factory=list)
    
    # Timestamp
    completed_at: Optional[str] = None


@dataclass
class CurrentPhaseContext:
    """Full context for the currently active phase.
    
    Unlike PreviousPhaseContext, this provides the complete payload
    to enable detailed, context-aware assistance during the active phase.
    
    Attributes:
        phase_name: Internal phase name
        phase_label: Human-friendly label
        status: Current status (IN_PROGRESS or COMPLETED)
        full_payload: Complete payload dict from BrainstormTask.payload_json
        task_id: Database ID of the current task
        task_title: Title of the current task
        duration_seconds: Allocated duration for this phase
        started_at: ISO timestamp when phase started
        documents: Documents available in this phase
    """
    phase_name: str
    phase_label: str
    status: PhaseStatus
    
    # Full payload from BrainstormTask.payload_json
    full_payload: Dict[str, Any] = field(default_factory=dict)
    
    # Task metadata
    task_id: Optional[int] = None
    task_title: Optional[str] = None
    duration_seconds: Optional[int] = None
    started_at: Optional[str] = None
    
    # Documents available in this phase
    documents: List[PhaseDocument] = field(default_factory=list)


@dataclass
class NextPhaseContext:
    """Preview of the upcoming phase for proactive assistance.
    
    Helps the Assistant provide forward-looking guidance like
    "Next we'll move to feasibility analysis which will evaluate clusters."
    
    Attributes:
        phase_name: Internal phase name
        phase_label: Human-friendly label
        depends_on: List of prerequisite phases (from task registry)
        expected_inputs: What data this phase needs
        expected_outputs: What data this phase produces
        description: Brief description of phase purpose
    """
    phase_name: str
    phase_label: str
    
    # Dependencies
    depends_on: List[str] = field(default_factory=list)
    
    # Expected inputs/outputs
    expected_inputs: List[str] = field(default_factory=list)
    expected_outputs: List[str] = field(default_factory=list)
    
    # Brief description
    description: str = ""


@dataclass
class PhaseContextBundle:
    """Complete phase awareness package for Assistant.
    
    This is the top-level container injected into AssistantContext,
    providing comprehensive phase awareness across the workshop lifecycle.
    
    Attributes:
        current_phase_index: 0-indexed position in TASK_SEQUENCE
        total_phases: Total number of phases in workshop
        task_sequence: Full ordered list of phase names
        previous_phases: Summaries of all completed phases
        current_phase: Full context for active phase
        next_phase: Preview of upcoming phase
        documents_by_phase: All documents grouped by phase
    """
    # Workshop progress
    current_phase_index: int
    total_phases: int
    task_sequence: List[str]
    
    # Phase contexts
    previous_phases: List[PreviousPhaseContext] = field(default_factory=list)
    current_phase: Optional[CurrentPhaseContext] = None
    next_phase: Optional[NextPhaseContext] = None
    
    # All documents indexed by phase
    documents_by_phase: Dict[str, List[PhaseDocument]] = field(default_factory=dict)
