from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    ActionItem,
    BrainstormTask,
    CapturedDecision,
    DiscussionNote,
    Document,
    Workshop,
    WorkshopDocument,
    WorkshopParticipant,
)

from app.assistant.memory.models import MemorySnippet
from app.assistant.time_context import TimeContextProvider
from app.assistant.phase_context_provider import PhaseContextProvider
from app.assistant.phase_context import PhaseContextBundle


@dataclass
class WorkshopEnvelope:
    id: int
    title: str
    objective: Optional[str]
    status: Optional[str]
    current_phase: Optional[str]
    current_task_id: Optional[int]
    current_task_title: Optional[str]
    duration: Optional[int]
    created_by_id: Optional[int]
    date_time: Optional[datetime]


@dataclass
class ParticipantEnvelope:
    id: int
    user_id: int
    role: str
    display_name: str


@dataclass
class DocumentEnvelope:
    id: int
    title: str
    description: Optional[str]
    file_path: Optional[str]
    summary: Optional[str]


@dataclass
class DecisionEnvelope:
    id: int
    topic: str
    decision: str
    rationale: Optional[str]
    owner_user_id: Optional[int]
    created_at: Optional[datetime]


@dataclass
class ActionItemEnvelope:
    id: int
    title: str
    owner_participant_id: Optional[int]
    due_date: Optional[datetime]
    description: Optional[str]
    success_metric: Optional[str]
    status: Optional[str]


@dataclass
class TranscriptExcerpt:
    ts: datetime
    speaker_user_id: Optional[int]
    text: str
    origin: str


@dataclass
class TimerSnapshot:
    phase_started_at: Optional[datetime] = None
    remaining_seconds: Optional[int] = None
    total_duration_seconds: Optional[int] = None


@dataclass
class PhaseSnapshots:
    framing: Optional[Dict[str, Any]] = None
    warm_up: Optional[Dict[str, Any]] = None
    brainstorming: Optional[Dict[str, Any]] = None
    clustering_voting: Optional[Dict[str, Any]] = None
    feasibility: Optional[Dict[str, Any]] = None
    prioritization: Optional[Dict[str, Any]] = None
    discussion: Optional[Dict[str, Any]] = None
    action_plan: Optional[Dict[str, Any]] = None
    summary: Optional[Dict[str, Any]] = None


@dataclass
class RBACContext:
    user_id: Optional[int]
    role: str
    is_facilitator: bool
    # Defaults added for backward compatibility with tests constructing RBACContext
    is_organizer: bool = False  # True if user created the workshop
    is_participant: bool = False  # True if user is an active participant


@dataclass
class AssistantContext:
    workshop: WorkshopEnvelope
    participants: List[ParticipantEnvelope] = field(default_factory=list)
    documents: List[DocumentEnvelope] = field(default_factory=list)
    decisions: List[DecisionEnvelope] = field(default_factory=list)
    action_items: List[ActionItemEnvelope] = field(default_factory=list)
    transcripts: List[TranscriptExcerpt] = field(default_factory=list)
    snapshots: PhaseSnapshots = field(default_factory=PhaseSnapshots)
    timers: TimerSnapshot = field(default_factory=TimerSnapshot)
    rbac: Optional[RBACContext] = None
    memory_snippets: List[MemorySnippet] = field(default_factory=list)
    temporal: Dict[str, Any] = field(default_factory=dict)
    time_alerts: List[str] = field(default_factory=list)
    available_tools: List[Dict[str, Any]] = field(default_factory=list)
    # Phase-aware context bundle (NEW)
    phase_bundle: Optional[PhaseContextBundle] = None


class ContextFabric:
    def __init__(self, recent_minutes: int = 15):
        self.recent_minutes = recent_minutes
        self.time_provider = TimeContextProvider()
        self.phase_provider = PhaseContextProvider()

    def build(self, workshop_id: int, user_id: Optional[int]) -> AssistantContext:
        workshop = db.session.get(
            Workshop,
            workshop_id,
        )
        if not workshop:
            raise ValueError(f"Workshop {workshop_id} not found")

        current_phase = getattr(workshop, "current_phase", None)
        if not current_phase and getattr(workshop, "current_task", None):
            current_phase = (
                getattr(workshop.current_task, "title", None)
                or getattr(workshop.current_task, "task_type", None)
            )

        workshop_env = WorkshopEnvelope(
            id=workshop.id,
            title=workshop.title,
            objective=getattr(workshop, "objective", None),
            status=getattr(workshop, "status", None),
            current_phase=current_phase,
            current_task_id=getattr(workshop, "current_task_id", None),
            current_task_title=getattr(getattr(workshop, "current_task", None), "title", None),
            duration=getattr(workshop, "duration", None),
            created_by_id=getattr(workshop, "created_by_id", None),
            date_time=getattr(workshop, "date_time", None),
        )

        participants = self._load_participants(workshop_id)
        documents = self._load_documents(workshop_id)
        decisions = self._load_decisions(workshop_id)
        action_items = self._load_action_items(workshop_id)
        transcripts = self._load_transcripts(workshop_id)
        snapshots = self._load_phase_snapshots(workshop_id)
        timers = self._derive_timer_snapshot(workshop)
        rbac = self._derive_rbac(workshop, user_id)
        temporal_context = self.time_provider.get_time_context(workshop_id)
        time_alerts: List[str] = []
        schedule = temporal_context.get("workshop_schedule", {}) if isinstance(temporal_context, dict) else {}
        remaining = schedule.get("remaining_minutes_in_phase") if isinstance(schedule, dict) else None
        overrun = schedule.get("phase_overrun_minutes") if isinstance(schedule, dict) else None
        if isinstance(remaining, int) and remaining >= 0 and remaining < 5:
            time_alerts.append("Phase ending soon")
        if isinstance(overrun, int) and overrun > 0:
            time_alerts.append("Phase time exceeded")

        # Build phase context bundle
        phase_bundle = self.phase_provider.build_phase_bundle(workshop)
        
        return AssistantContext(
            workshop=workshop_env,
            participants=participants,
            documents=documents,
            decisions=decisions,
            action_items=action_items,
            transcripts=transcripts,
            snapshots=snapshots,
            timers=timers,
            rbac=rbac,
            temporal=temporal_context,
            time_alerts=time_alerts,
            phase_bundle=phase_bundle,
        )

    def _load_participants(self, workshop_id: int) -> List[ParticipantEnvelope]:
        rows = (
            db.session.query(WorkshopParticipant)
            .options(joinedload(WorkshopParticipant.user))
            .filter(WorkshopParticipant.workshop_id == workshop_id)
            .all()
        )
        out: List[ParticipantEnvelope] = []
        for row in rows:
            user = row.user
            display = getattr(user, "display_name", None)
            out.append(
                ParticipantEnvelope(
                    id=row.id,
                    user_id=row.user_id,
                    role=row.role or "participant",
                    display_name=display or getattr(user, "email", "unknown"),
                )
            )
        return out

    def _load_documents(self, workshop_id: int) -> List[DocumentEnvelope]:
        links = (
            db.session.query(WorkshopDocument)
            .options(joinedload(WorkshopDocument.document))
            .filter(WorkshopDocument.workshop_id == workshop_id)
            .order_by(WorkshopDocument.added_at.desc())
            .all()
        )
        out: List[DocumentEnvelope] = []
        for link in links:
            doc: Document = link.document
            if not doc:
                continue
            out.append(
                DocumentEnvelope(
                    id=doc.id,
                    title=doc.title,
                    description=doc.description,
                    file_path=doc.file_path,
                    summary=getattr(doc, "summary", None),
                )
            )
        return out

    def _load_decisions(self, workshop_id: int) -> List[DecisionEnvelope]:
        rows = (
            db.session.query(CapturedDecision)
            .filter(CapturedDecision.workshop_id == workshop_id)
            .order_by(CapturedDecision.created_at.desc())
            .limit(25)
            .all()
        )
        return [
            DecisionEnvelope(
                id=row.id,
                topic=row.topic,
                decision=row.decision,
                rationale=row.rationale,
                owner_user_id=row.owner_user_id,
                created_at=row.created_at,
            )
            for row in rows
        ]

    def _load_action_items(self, workshop_id: int) -> List[ActionItemEnvelope]:
        rows = (
            db.session.query(ActionItem)
            .filter(ActionItem.workshop_id == workshop_id)
            .order_by(ActionItem.created_at.desc())
            .limit(50)
            .all()
        )
        out: List[ActionItemEnvelope] = []
        for row in rows:
            out.append(
                ActionItemEnvelope(
                    id=row.id,
                    title=row.title,
                    owner_participant_id=getattr(row, "owner_participant_id", None),
                    due_date=getattr(row, "due_date", None),
                    description=getattr(row, "description", None),
                    success_metric=getattr(row, "success_metric", None),
                    status=getattr(row, "status", None),
                )
            )
        return out

    def _load_transcripts(self, workshop_id: int) -> List[TranscriptExcerpt]:
        cutoff = datetime.utcnow() - timedelta(minutes=self.recent_minutes)
        rows = (
            db.session.query(DiscussionNote)
            .filter(DiscussionNote.workshop_id == workshop_id)
            .filter(DiscussionNote.ts >= cutoff)
            .order_by(DiscussionNote.ts.desc())
            .limit(50)
            .all()
        )
        return [
            TranscriptExcerpt(
                ts=row.ts,
                speaker_user_id=row.speaker_user_id,
                text=row.point,
                origin=row.origin,
            )
            for row in rows
        ]

    def _load_phase_snapshots(self, workshop_id: int) -> PhaseSnapshots:
        task_types = {
            "framing": "framing",
            "warm_up": "warm-up",
            "brainstorming": "brainstorming",
            "clustering_voting": "clustering_voting",
            "feasibility": "results_feasibility",
            "prioritization": "results_prioritization",
            "discussion": "results_discussion",
            "action_plan": "results_action_plan",
            "summary": "summary",
        }
        payloads: Dict[str, Optional[Dict[str, Any]]] = {}
        for key, task_type in task_types.items():
            record = (
                db.session.query(BrainstormTask)
                .filter(BrainstormTask.workshop_id == workshop_id)
                .filter(BrainstormTask.task_type == task_type)
                .order_by(BrainstormTask.created_at.desc())
                .first()
            )
            payloads[key] = self._safe_json(record.payload_json) if record else None
        return PhaseSnapshots(**payloads)

    def _derive_timer_snapshot(self, workshop: Workshop) -> TimerSnapshot:
        started_at = (
            getattr(workshop, "phase_started_at", None)
            or getattr(workshop, "current_task_started_at", None)
            or getattr(workshop, "current_task_start_time", None)
            or getattr(workshop, "timer_start_time", None)
        )
        remaining = getattr(workshop, "current_task_remaining", None)
        total = None
        if workshop.current_task_id:
            task = db.session.get(BrainstormTask, workshop.current_task_id)
            if task:
                total = getattr(task, "duration", None)

        if remaining is None:
            try:
                computed_remaining = workshop.get_remaining_task_time()
            except Exception:  # pragma: no cover - defensive against legacy models
                computed_remaining = None
            if computed_remaining is not None:
                remaining = computed_remaining

        if remaining is None and total:
            elapsed_before_pause = getattr(workshop, "timer_elapsed_before_pause", 0) or 0
            timer_paused_at = getattr(workshop, "timer_paused_at", None)
            timer_start_time = getattr(workshop, "timer_start_time", None)
            elapsed = None
            if timer_paused_at:
                elapsed = elapsed_before_pause
            elif timer_start_time:
                elapsed = elapsed_before_pause + max(
                    (datetime.utcnow() - timer_start_time).total_seconds(),
                    0,
                )
            if elapsed is not None:
                remaining = max(int(total - elapsed), 0)

        return TimerSnapshot(
            phase_started_at=started_at,
            remaining_seconds=remaining,
            total_duration_seconds=total,
        )

    def _derive_rbac(self, workshop: Workshop, user_id: Optional[int]) -> Optional[RBACContext]:
        """
        Derive RBAC context for the user.
        
        - is_organizer: True if user created the workshop (workshop.created_by_id == user_id)
        - is_participant: True if user has a WorkshopParticipant record
        - is_facilitator: True if role is organizer/facilitator/admin
        """
        if user_id is None:
            return None
        
        # Check if user is the workshop organizer (creator)
        is_organizer = getattr(workshop, "created_by_id", None) == user_id
        
        # Check if user is a participant
        participant = (
            db.session.query(WorkshopParticipant)
            .filter(WorkshopParticipant.workshop_id == workshop.id)
            .filter(WorkshopParticipant.user_id == user_id)
            .first()
        )
        
        if not participant:
            # Not a participant - could be organizer or guest
            if is_organizer:
                return RBACContext(
                    user_id=user_id,
                    role="organizer",
                    is_facilitator=True,
                    is_organizer=True,
                    is_participant=False
                )
            return RBACContext(
                user_id=user_id,
                role="guest",
                is_facilitator=False,
                is_organizer=False,
                is_participant=False
            )
        
        # User is a participant
        role = participant.role or "participant"
        is_facilitator = role in {"organizer", "facilitator", "admin"}
        
        return RBACContext(
            user_id=user_id,
            role=role,
            is_facilitator=is_facilitator,
            is_organizer=is_organizer,
            is_participant=True
        )

    @staticmethod
    def _safe_json(value: Optional[str]) -> Optional[Dict[str, Any]]:
        if not value:
            return None
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return None
