"""Phase context provider for Assistant LLM.

This module builds comprehensive phase context bundles by:
1. Loading phase payloads from BrainstormTask.payload_json
2. Extracting key artifacts per phase type
3. Generating narrative summaries
4. Linking documents to phases
5. Providing previous/current/next phase awareness
"""

from __future__ import annotations
import json
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.models import Workshop, BrainstormTask, Document, WorkshopDocument
from app.extensions import db
from app.config import TASK_SEQUENCE
from app.tasks.registry import TASK_REGISTRY
from app.assistant.phase_context import (
    PhaseContextBundle,
    PreviousPhaseContext,
    CurrentPhaseContext,
    NextPhaseContext,
    PhaseDocument,
    PhaseStatus,
)


class PhaseContextProvider:
    """Builds comprehensive phase context for Assistant LLM.
    
    Usage:
        provider = PhaseContextProvider()
        bundle = provider.build_phase_bundle(workshop)
        # Use bundle in AssistantContext
    """
    
    # Phase display labels (user-facing names)
    PHASE_LABELS = {
        "framing": "Briefing",
        "warm-up": "Warm-up",
        "warm_up": "Warm-up",
        "brainstorming": "Ideas",
        "clustering_voting": "Clustering",
        "results_feasibility": "Feasibility",
        "results_prioritization": "Prioritization",
        "discussion": "Discussion",
        "results_action_plan": "Action Plan",
        "summary": "Summary",
    }
    
    # Document type mapping
    DOCUMENT_TYPES = {
        "framing": "framing_brief",
        "results_feasibility": "feasibility_report",
        "results_prioritization": "prioritization_shortlist",
        "results_action_plan": "action_plan",
        "summary": "summary_report",
    }
    
    def build_phase_bundle(self, workshop: Workshop) -> PhaseContextBundle:
        """Build complete phase context bundle for a workshop.
        
        Args:
            workshop: Workshop instance
            
        Returns:
            PhaseContextBundle with previous/current/next contexts
        """
        current_phase_name = self._get_current_phase(workshop)
        current_index = self._get_phase_index(current_phase_name)
        
        # Load all completed tasks
        tasks = self._load_workshop_tasks(workshop.id)
        
        # Load all documents with phase timing
        documents = self._load_phase_documents(workshop.id, tasks)
        
        # Build previous phases
        previous_phases = []
        for i in range(current_index):
            phase_name = TASK_SEQUENCE[i]
            task = tasks.get(phase_name)
            if task and task.status == "completed":
                previous = self._build_previous_phase(phase_name, task, documents)
                previous_phases.append(previous)
        
        # Build current phase
        current_phase = None
        if current_phase_name:
            current_task = tasks.get(current_phase_name)
            if current_task:
                current_phase = self._build_current_phase(
                    current_phase_name, current_task, documents
                )
        
        # Build next phase
        next_phase = None
        if current_index + 1 < len(TASK_SEQUENCE):
            next_phase_name = TASK_SEQUENCE[current_index + 1]
            next_phase = self._build_next_phase(next_phase_name)
        
        # Group documents by phase
        docs_by_phase: Dict[str, List[PhaseDocument]] = {}
        for doc in documents:
            if doc.phase not in docs_by_phase:
                docs_by_phase[doc.phase] = []
            docs_by_phase[doc.phase].append(doc)
        
        return PhaseContextBundle(
            current_phase_index=current_index,
            total_phases=len(TASK_SEQUENCE),
            task_sequence=TASK_SEQUENCE,
            previous_phases=previous_phases,
            current_phase=current_phase,
            next_phase=next_phase,
            documents_by_phase=docs_by_phase,
        )
    
    def _get_current_phase(self, workshop: Workshop) -> Optional[str]:
        """Get current phase name from workshop."""
        if workshop.current_task:
            return workshop.current_task.task_type
        return workshop.current_phase
    
    def _get_phase_index(self, phase_name: Optional[str]) -> int:
        """Get index of phase in sequence."""
        if not phase_name:
            return 0
        try:
            return TASK_SEQUENCE.index(phase_name)
        except ValueError:
            return 0
    
    def _load_workshop_tasks(self, workshop_id: int) -> Dict[str, BrainstormTask]:
        """Load all tasks for workshop indexed by task_type."""
        tasks = (
            BrainstormTask.query
            .filter_by(workshop_id=workshop_id)
            .order_by(BrainstormTask.created_at.asc())
            .all()
        )
        
        # Index by task_type, keeping most recent
        by_type: Dict[str, BrainstormTask] = {}
        for task in tasks:
            by_type[task.task_type] = task
        
        return by_type
    
    def _load_phase_documents(
        self,
        workshop_id: int,
        tasks: Dict[str, BrainstormTask],
    ) -> List[PhaseDocument]:
        """Load all documents with phase association based on creation timestamp.
        
        Strategy:
        1. For each document, find the phase task that created it by comparing timestamps
        2. Document is linked to the phase where it was created (nearest task.ended_at)
        3. Fallback to title/type inference if timestamp matching fails
        """
        links = (
            WorkshopDocument.query
            .filter_by(workshop_id=workshop_id)
            .order_by(WorkshopDocument.added_at.desc())
            .all()
        )
        
        documents: List[PhaseDocument] = []
        for link in links:
            doc: Optional[Document] = link.document
            if not doc:
                continue
            
            # Try to infer phase from document creation timestamp
            phase = self._infer_document_phase_by_timestamp(doc, tasks, link)
            if not phase:
                # Fallback to title/type inference
                phase = self._infer_document_phase_by_title(doc)
            
            doc_type = self._infer_document_type(doc, phase)
            
            documents.append(
                PhaseDocument(
                    id=doc.id,
                    title=doc.title or "Untitled",
                    url=self._get_document_url(doc),
                    phase=phase,
                    document_type=doc_type,
                    summary=self._truncate(doc.summary, 200),
                )
            )
        
        return documents
    
    def _infer_document_phase_by_timestamp(
        self,
        doc: Document,
        tasks: Dict[str, BrainstormTask],
        link: WorkshopDocument,
    ) -> Optional[str]:
        """Infer phase by finding the task whose ended_at is closest before doc creation."""
        doc_created = link.added_at or doc.uploaded_at
        if not doc_created:
            return None
        
        # Find tasks that ended before document was created
        candidates = []
        for phase_name, task in tasks.items():
            if task.ended_at and task.ended_at <= doc_created:
                time_diff = (doc_created - task.ended_at).total_seconds()
                # Only consider if document was created within 5 minutes after task ended
                if time_diff <= 300:  # 5 minutes
                    candidates.append((phase_name, time_diff))
        
        if candidates:
            # Return phase with smallest time difference
            candidates.sort(key=lambda x: x[1])
            return candidates[0][0]
        
        return None
    
    def _infer_document_phase_by_title(self, doc: Document) -> str:
        """Infer which phase generated this document based on title."""
        title_lower = (doc.title or "").lower()
        
        if "framing" in title_lower or "brief" in title_lower:
            return "framing"
        if "feasibility" in title_lower:
            return "results_feasibility"
        if "prioritization" in title_lower or "shortlist" in title_lower:
            return "results_prioritization"
        if "action" in title_lower or "plan" in title_lower:
            return "results_action_plan"
        if "summary" in title_lower:
            return "summary"
        
        return "framing"  # Default fallback
    
    def _build_previous_phase(
        self,
        phase_name: str,
        task: BrainstormTask,
        documents: List[PhaseDocument],
    ) -> PreviousPhaseContext:
        """Build summary context for a completed phase."""
        payload = self._parse_payload(task.payload_json)
        phase_label = self.PHASE_LABELS.get(phase_name, phase_name.title())
        
        # Extract key artifacts based on phase type
        key_artifacts = self._extract_key_artifacts(phase_name, payload)
        
        # Build narrative summary
        summary = self._build_phase_summary(phase_name, payload, key_artifacts)
        
        # Filter documents for this phase
        phase_docs = [d for d in documents if d.phase == phase_name]
        
        completed_at = None
        if task.ended_at:
            completed_at = task.ended_at.isoformat()
        
        return PreviousPhaseContext(
            phase_name=phase_name,
            phase_label=phase_label,
            status=PhaseStatus.COMPLETED,
            summary=summary,
            key_artifacts=key_artifacts,
            documents=phase_docs,
            completed_at=completed_at,
        )
    
    def _build_current_phase(
        self,
        phase_name: str,
        task: BrainstormTask,
        documents: List[PhaseDocument],
    ) -> CurrentPhaseContext:
        """Build full context for current phase."""
        payload = self._parse_payload(task.payload_json)
        phase_label = self.PHASE_LABELS.get(phase_name, phase_name.title())
        
        status = PhaseStatus.IN_PROGRESS
        if task.status == "completed":
            status = PhaseStatus.COMPLETED
        elif task.status == "pending":
            status = PhaseStatus.NOT_STARTED
        
        phase_docs = [d for d in documents if d.phase == phase_name]
        
        started_at = None
        if task.started_at:
            started_at = task.started_at.isoformat()
        
        return CurrentPhaseContext(
            phase_name=phase_name,
            phase_label=phase_label,
            status=status,
            full_payload=payload,
            task_id=task.id,
            task_title=task.title,
            duration_seconds=task.duration,
            started_at=started_at,
            documents=phase_docs,
        )
    
    def _build_next_phase(self, phase_name: str) -> NextPhaseContext:
        """Build preview for upcoming phase."""
        phase_label = self.PHASE_LABELS.get(phase_name, phase_name.title())
        
        # Get metadata from registry
        meta = TASK_REGISTRY.get(phase_name, {})
        depends_on = meta.get("inputs", [])
        expected_outputs = meta.get("outputs", [])
        
        # Build description
        description = self._get_phase_description(phase_name)
        
        return NextPhaseContext(
            phase_name=phase_name,
            phase_label=phase_label,
            depends_on=depends_on,
            expected_inputs=depends_on,
            expected_outputs=expected_outputs,
            description=description,
        )
    
    def _extract_key_artifacts(
        self,
        phase_name: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Extract key artifacts from phase payload for summary."""
        artifacts: Dict[str, Any] = {}
        
        if phase_name == "framing":
            artifacts["problem_statement"] = payload.get("problem_statement")
            artifacts["success_criteria"] = payload.get("success_criteria", [])
            artifacts["assumptions"] = payload.get("assumptions", [])
            artifacts["constraints"] = payload.get("constraints", [])
        
        elif phase_name in ("warm-up", "warm_up"):
            artifacts["selected_option"] = payload.get("selected_option")
            artifacts["energy_level"] = payload.get("energy_level")
        
        elif phase_name == "brainstorming":
            ideas = payload.get("ai_seed_ideas", [])
            artifacts["ai_seed_count"] = len(ideas) if isinstance(ideas, list) else 0
        
        elif phase_name == "clustering_voting":
            clusters = payload.get("clusters", [])
            artifacts["cluster_count"] = len(clusters)
            if clusters:
                # Top cluster by votes
                top = max(clusters, key=lambda c: c.get("votes", 0), default=None)
                if top:
                    artifacts["top_cluster"] = {
                        "name": top.get("name"),
                        "votes": top.get("votes", 0),
                    }
        
        elif phase_name == "results_feasibility":
            artifacts["rubrics"] = payload.get("feasibility_rules_and_rubrics")
            evals = payload.get("cluster_evaluations", [])
            artifacts["evaluation_count"] = len(evals)
        
        elif phase_name == "results_prioritization":
            shortlist = payload.get("shortlist", [])
            artifacts["shortlist_count"] = len(shortlist)
        
        elif phase_name == "results_action_plan":
            milestones = payload.get("milestones", [])
            actions = payload.get("action_items_list", [])
            artifacts["milestone_count"] = len(milestones)
            artifacts["action_item_count"] = len(actions)
        
        return artifacts
    
    def _build_phase_summary(
        self,
        phase_name: str,
        payload: Dict[str, Any],
        artifacts: Dict[str, Any],
    ) -> str:
        """Build 2-3 sentence narrative summary of phase."""
        if phase_name == "framing":
            problem = artifacts.get("problem_statement", "").strip()
            if problem:
                return f"Workshop briefing established the problem statement: {self._truncate(problem, 120)}. Success criteria and constraints were defined."
            return "Workshop briefing completed with problem framing and success criteria."
        
        elif phase_name in ("warm-up", "warm_up"):
            option = artifacts.get("selected_option")
            if option:
                return f"Warm-up activity completed using '{option}' to energize participants."
            return "Warm-up activity completed to prepare participants."
        
        elif phase_name == "brainstorming":
            ai_count = artifacts.get("ai_seed_count", 0)
            if ai_count > 0:
                return f"Brainstorming phase generated ideas, including {ai_count} AI-seeded suggestions to spark creativity."
            return "Brainstorming phase completed with participant ideas collected."
        
        elif phase_name == "clustering_voting":
            count = artifacts.get("cluster_count", 0)
            top = artifacts.get("top_cluster")
            if top:
                return f"Ideas clustered into {count} themes. Top cluster by votes: '{top.get('name')}' with {top.get('votes')} votes."
            return f"Ideas clustered into {count} themes for voting."
        
        elif phase_name == "results_feasibility":
            eval_count = artifacts.get("evaluation_count", 0)
            return f"Feasibility analysis completed for {eval_count} clusters using defined rubrics."
        
        elif phase_name == "results_prioritization":
            shortlist_count = artifacts.get("shortlist_count", 0)
            return f"Prioritization completed with {shortlist_count} items ranked by impact and effort."
        
        elif phase_name == "results_action_plan":
            milestones = artifacts.get("milestone_count", 0)
            actions = artifacts.get("action_item_count", 0)
            return f"Action plan created with {milestones} milestones and {actions} action items assigned."
        
        return f"{phase_name.title()} phase completed successfully."
    
    def _get_phase_description(self, phase_name: str) -> str:
        """Get description for upcoming phase."""
        descriptions = {
            "framing": "Establish problem statement, success criteria, and workshop objectives.",
            "warm-up": "Energize participants and set collaborative tone.",
            "warm_up": "Energize participants and set collaborative tone.",
            "brainstorming": "Collect ideas and creative solutions from participants.",
            "clustering_voting": "Group ideas into themes and vote on priorities.",
            "results_feasibility": "Analyze feasibility of top clusters using defined rubrics.",
            "results_prioritization": "Prioritize solutions by impact and effort.",
            "discussion": "Facilitate open discussion on results and decisions.",
            "results_action_plan": "Create actionable roadmap with milestones and assignments.",
            "summary": "Summarize workshop outcomes and next steps.",
        }
        return descriptions.get(phase_name, "")
    
    def _infer_document_type(self, doc: Document, phase: str) -> str:
        """Infer document type."""
        return self.DOCUMENT_TYPES.get(phase, "report")
    
    def _get_document_url(self, doc: Document) -> Optional[str]:
        """Get public URL for document."""
        if hasattr(doc, 'url') and doc.url:
            return doc.url
        if doc.file_path:
            # Convert file_path to URL
            if doc.file_path.startswith("uploads/reports/"):
                return f"/media/reports/{doc.file_path.split('/')[-1]}"
        return None
    
    def _parse_payload(self, payload_json: Optional[str]) -> Dict[str, Any]:
        """Parse payload JSON safely."""
        if not payload_json:
            return {}
        try:
            parsed = json.loads(payload_json)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    
    def _truncate(self, text: Optional[str], max_len: int) -> Optional[str]:
        """Truncate text to max length."""
        if not text:
            return None
        text = text.strip()
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + "..."
