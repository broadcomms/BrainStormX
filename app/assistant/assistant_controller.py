from __future__ import annotations

import json
import textwrap
import uuid
from typing import Any, Dict, List, Tuple, Optional, Set

from flask import Blueprint, current_app, request
from flask.typing import ResponseReturnValue
from flask_login import current_user
from pydantic import ValidationError

from app.assistant.context import AssistantContext, ContextFabric, RBACContext, TimerSnapshot
from app.assistant.persona import PersonaConfig, PersonaRouter
from app.assistant.registry import TOOL_REGISTRY
from app.assistant.schemas import (
    AssistantCitationPayload,
    AssistantFeedbackPayload,
    AssistantQuery,
    AssistantReply,
    AssistantToolCall,
    PersonaType,
)
from app.assistant.tooling import ToolExecutor
from app.assistant.tools.factory import build_default_registry
from app.assistant.tools.gateway import ToolGateway
from app.assistant.memory import AgentCoreMemorySettings, AgentMemoryService, NullMemoryService
from app.extensions import db
from app.models import Document
from app.models_assistant import AssistantCitation, AssistantMessageFeedback, ChatThread, ChatTurn
from app.utils.json_utils import extract_json_block
from app.utils.llm_bedrock import get_chat_llm_pro

bp = Blueprint("assistant", __name__, url_prefix="/assistant")


class AssistantLLMClient:
    def __init__(self) -> None:
        self.client = get_chat_llm_pro(
            model_kwargs={
                "temperature": 0.2,
                "max_tokens": 900,
                "stream": False,
            }
        )

    def plan(self, persona: PersonaType, context: AssistantContext, query: AssistantQuery) -> AssistantReply:
        prompt = self._compose_plan_prompt(persona, context, query)
        raw = self.client.invoke(prompt)
        payload = self._parse_json_payload(raw)
        return AssistantReply.model_validate_json(payload)

    def compose(
        self,
        persona: PersonaType,
        context: AssistantContext,
        query: AssistantQuery,
        plan: AssistantReply,
        tool_results: List[Dict[str, Any]],
    ) -> AssistantReply:
        prompt = self._compose_response_prompt(persona, context, query, plan, tool_results)
        raw = self.client.invoke(prompt)
        payload = self._parse_json_payload(raw)
        return AssistantReply.model_validate_json(payload)

    @staticmethod
    def _parse_json_payload(raw: Any) -> str:
        text = str(getattr(raw, "content", raw))
        json_blob = extract_json_block(text) or text
        try:
            payload = json.loads(json_blob)
        except json.JSONDecodeError:
            # Respect strict JSON mode: bubble up invalid JSON
            from flask import current_app as _app
            if getattr(_app.config, "ASSISTANT_STRICT_JSON", True):
                raise
            return json_blob

        if isinstance(payload, dict):
            speech = payload.get("speech")
            if isinstance(speech, str):
                payload["speech"] = {"text": speech}
            elif speech is None:
                payload.pop("speech", None)
            elif not isinstance(speech, dict):
                payload["speech"] = {"text": str(speech)}

            # Normalize ui_hints to a dict (AssistantReply expects an object)
            ui_hints = payload.get("ui_hints", None)
            if isinstance(ui_hints, list):
                # Wrap list into a notes field to preserve content
                payload["ui_hints"] = {"notes": ui_hints}
            elif isinstance(ui_hints, str):
                payload["ui_hints"] = {"note": ui_hints}
            elif ui_hints is None:
                payload.pop("ui_hints", None)
            elif not isinstance(ui_hints, dict):
                payload["ui_hints"] = {"value": str(ui_hints)}

            # Do not fabricate reply text; rely on LLM to provide valid content

            return json.dumps(payload, ensure_ascii=False)

        return json_blob

    def _compose_plan_prompt(
        self,
        persona: PersonaType,
        context: AssistantContext,
        query: AssistantQuery,
    ) -> str:
        primer = PersonaRouter().get_primer(persona) or "You are a helpful workshop assistant."
        context_text = PromptBuilder.compact_context(context)
        temporal = getattr(context, "temporal", {}) or {}
        schedule = temporal.get("workshop_schedule") if isinstance(temporal, dict) else {}
        temporal_block = ""
        if temporal:
            temporal_block = "Current local time: {local} ({tz}).".format(
                local=temporal.get("current_time_local", "unknown"),
                tz=temporal.get("local_timezone", "UTC"),
            )
        if isinstance(schedule, dict) and schedule:
            remaining = schedule.get("remaining_minutes_in_phase")
            if isinstance(remaining, int):
                temporal_block += f" Remaining minutes in phase: {remaining}."
            overrun = schedule.get("phase_overrun_minutes")
            if isinstance(overrun, int) and overrun > 0:
                temporal_block += f" Phase is over time by {overrun} minutes."
        if getattr(context, "time_alerts", None):
            temporal_block += " Alerts: " + "; ".join(context.time_alerts)

        available_tools = getattr(context, "available_tools", [])
        tools_json = json.dumps(available_tools, ensure_ascii=False)

        # Hint blocks defined later before returning the final prompt

        # Add workshop control command instructions based on user role
        rbac = getattr(context, "rbac", None)
        control_instructions = ""
        if rbac and getattr(rbac, "is_organizer", False):
            control_instructions = """
You can execute workshop control actions via tools:

Organizer Commands (you have permission):
- "Begin the workshop" / "Start the workshop" → Use workshop_control.begin_workshop tool
- "Go to next phase" / "Next task" / "Move to next step" / "Advance to next task" → Use workshop_control.next_task tool
- "End this task" / "End current task" / "Finish this phase" → Use workshop_control.end_current_task tool
- "Pause the workshop" → Use workshop_control.pause_workshop tool
- "Resume the workshop" → Use workshop_control.resume_workshop tool
- "Stop the workshop" / "End the workshop" → Use workshop_control.stop_workshop tool

Participant Actions (you can also do these):
- "Add idea: [text]" / "I have an idea: [text]" → Use workshop_control.add_idea tool
  - Call as: {"name": "workshop_control.add_idea", "args": {"text": "<the idea text>"}}

- "Vote for [cluster name]" / "Cast vote for [cluster]" → Call {"name": "workshop.vote_for_cluster", "args": {"cluster_name": "<cluster name>"}}
  - The tool returns vote totals and remaining dots. Confirm the outcome using that data.

When the user gives one of these commands, execute the corresponding tool and confirm the action.
"""
        elif rbac and getattr(rbac, "is_participant", False):
            control_instructions = """
You can execute participant actions via tools:

Participant Commands (you have permission):
- "Add idea: [text]" / "I have an idea: [text]" → Use workshop_control.add_idea tool
  - Call as: {"name": "workshop_control.add_idea", "args": {"text": "<the idea text>"}}
  - Do NOT nest or rename the field (use exactly key "text"). The system will add workshop_id and user_id.

- "Vote for [cluster name]" / "Cast vote for [cluster]" → Call {"name": "workshop.vote_for_cluster", "args": {"cluster_name": "<cluster name>"}}
  - Use the tool response to confirm the vote and communicate remaining dots if relevant.

If the user tries organizer commands, politely explain that only the organizer can execute those actions.
"""

        # Always return a composed prompt (for any role)
        # Re-define hint blocks here to ensure they are in local scope for the f-string below
        cluster_hint = (
            "\nIf the user asks about idea clusters, clustering results, or to list/show clusters, "
            "call the tool named \"workshop.list_clusters\" to retrieve the latest clusters with names, "
            "descriptions, and votes before composing your answer.\n"
        )
        agenda_hint = (
            "\nIf the user asks for the agenda, session outline, planned phases, or time allocations for this workshop, "
            "call the tool named \"workshop.get_agenda\" to retrieve the saved agenda items (titles, descriptions, durations) "
            "before composing your answer.\n"
        )
        ideas_hint = (
            "\nIf the user asks for the individual ideas (all items) from brainstorming, or what ideas have been submitted, "
            "call the tool named \"workshop.list_ideas\". During brainstorming/warm-up phases, this returns unclustered ideas. "
            "After clustering, ideas are grouped by cluster. "
            "IMPORTANT: If the tool returns ideas in the 'ideas' array with 'total_count' > 0, list them in your response. "
            "If it returns empty arrays but tool succeeded, say 'No ideas have been submitted yet.' "
            "If they reference a specific cluster, pass cluster_id to get only that cluster's ideas.\n"
        )
        reports_hint = (
            "\nIf the user asks to list documents, reports, files, or attachments for this workshop — including feasibility, "
            "framing brief, prioritization shortlist, action plan, or summary — call the tool named \"workshop.list_reports\" "
            "(optionally filter by phase) to retrieve the latest document URLs and metadata before composing your answer.\n"
        )
        read_hint = (
            "\nIf the user asks to summarize, read, quote, or analyze the content of a specific report/document, first locate it via "
            "\"workshop.list_reports\", then call \"workshop.read_report\" with the document_id (integer ID, NOT the URL path) to retrieve the text content "
            "to ground your response. IMPORTANT: Use the numeric Document ID shown in phase context (e.g., 'Document ID 123'), not the file path/URL. "
            "Keep summaries concise unless asked for detailed analysis.\n"
        )
        identity_hint = (
            "\nIdentity Q&A: If the user asks 'what is my name' or 'what is my role', answer ONLY using RBAC and participants from context. "
            "If the display name or role is not present, say you don't know rather than guessing. Do not fabricate identity details.\n"
        )
        
        # Phase-aware contextual hints
        phase_hints = self._build_phase_hints(context)
        
        return textwrap.dedent(
            f"""
            System: {primer}\n
            You answer as JSON with keys: role, persona, text, citations, tool_calls, proposed_actions, ui_hints.
            Persona: {persona.value}
            Workshop Context:\n{context_text}
            {phase_hints}
            Temporal Context: {temporal_block or 'No temporal data provided'}
            Available tools:\n{tools_json}
            {control_instructions}
            {cluster_hint}
            {agenda_hint}
            {ideas_hint}
            {reports_hint}
            {read_hint}
            {identity_hint}
            User: {query.text}
            Respond with strict JSON. Use tool_calls when you need extra data.
            
            TIME TOOLS GUIDANCE:
            - For time.get_phase_timing: Use workshop_id from context (Workshop ID: {context.workshop.id})
            - The tool returns timing for the CURRENT phase automatically - do NOT pass phase name
            - Example: {{"name": "time.get_phase_timing", "args": {{"workshop_id": {context.workshop.id}}}}}
            
            For other time.* tools (start_timer, schedule_reminder, query_recent_activity):
            - All require workshop_id parameter: {context.workshop.id}

            Important: For proposed_actions, provide user-friendly descriptions:
            - Each action should have an "action" field with a clear, natural description (e.g., "Start the workshop timer")
            - NOT internal tool names (e.g., NOT "Time.start Timer" or "time.start_timer")
            - Add optional "time_estimate" for action duration
            Example: {{"action": "Start the workshop timer", "time_estimate": "10 minutes"}}
            """
        ).strip()

    def _compose_response_prompt(
        self,
        persona: PersonaType,
        context: AssistantContext,
        query: AssistantQuery,
        plan: AssistantReply,
        tool_results: List[Dict[str, Any]],
    ) -> str:
        primer = PersonaRouter().get_primer(persona) or "You are a helpful workshop assistant."
        context_text = PromptBuilder.compact_context(context)
        tools_json = json.dumps(tool_results, ensure_ascii=False)
        plan_json = json.dumps(plan.model_dump(), ensure_ascii=False)
        identity_hint = (
            "\nIdentity Q&A: If asked 'what is my name' or 'what is my role', answer ONLY using RBAC and participants from context. "
            "If unknown, state that you don't have that information. Do not guess.\n"
        )
        return textwrap.dedent(
            f"""
            System: {primer}
            Persona: {persona.value}
            Workshop Context:\n{context_text}
            Original User Request: {query.text}
            Planning JSON: {plan_json}
            Tool Outputs: {tools_json}
            Compose the final strict JSON reply with grounded citations, persona field, proposed_actions, and ui_hints.
            Always include a non-empty "text" field with the assistant's message. Do not omit it.
            {identity_hint}
            If you used the tool "workshop.list_clusters", include a short bulleted list of the clusters:
            - Use the cluster name as the main label
            - Add a concise gist or description
            - Include counts, e.g., (ideas: N, votes: V)
            If you used the tool "workshop.vote_for_cluster", confirm the vote with the returned totals:
            - Mention the cluster name and its updated vote count
            - Indicate how many dots the user has remaining when available
            If you used the tool "workshop.get_agenda", present the agenda clearly:
            - Use a numbered or bulleted list in chronological order
            - Include each item's title and, when available, the estimated minutes in parentheses (e.g., "(7 min)")
            - Add short descriptions when provided in the tool output
            If you used the tool "workshop.list_ideas", present the granular ideas clearly:
            - If grouped by cluster, show a bullet per cluster, then nested bullets for each idea text (cap at reasonable length)
            - If a single cluster was requested, list the ideas as top-level bullets
            - Do not fabricate ideas; only include items returned by the tool
            If you used the tool "workshop.list_reports", include a short bulleted list of the returned documents with clickable links:
            - Use the document title as the link text
            - Link to the provided URL for each document
            - Optionally annotate the phase in parentheses (e.g., (feasibility))
            If the user requested a summary of a specific report, use "workshop.read_report" to retrieve the text and then provide a concise summary in the "text" field with 3-6 bullet points. Include a citation to the document.
            Include action_buttons inside ui_hints when they would help the facilitator.
            
            Important: For proposed_actions, use clear, user-friendly descriptions:
            - "action" field should be natural language (e.g., "Start the workshop timer", "Prepare session materials")
            - NOT internal tool names (e.g., NOT "Time.start Timer" or "time.start_timer")
            - Add helpful context in "time_estimate" or "description" fields
            """
        ).strip()

    def _build_phase_hints(self, context: AssistantContext) -> str:
        """Build phase-specific contextual hints for the LLM.
        
        Provides guidance based on the current workshop phase to help the LLM
        provide more relevant and accurate responses using available phase context.
        
        Args:
            context: The full assistant context including phase_bundle
            
        Returns:
            Formatted string with phase-specific hints, or empty string if no phase context
        """
        if not context.phase_bundle or not context.phase_bundle.current_phase:
            return ""
        
        current = context.phase_bundle.current_phase
        hints: List[str] = []
        
        hints.append("\n=== PHASE-SPECIFIC GUIDANCE ===")
        
        if current.phase_name == "framing":
            hints.append(
                "You have access to the complete workshop briefing including problem statement, "
                "success criteria, assumptions, and constraints. Reference these directly when "
                "answering questions about workshop goals, scope, or objectives. The problem "
                "statement is the definitive source for 'what problem are we solving?'"
            )
        
        elif current.phase_name == "warm-up":
            hints.append(
                "The warm-up phase is active. You can reference the selected warm-up activity, "
                "energy level, and participation norms that were established. Focus on helping "
                "participants engage and build team rapport."
            )
        
        elif current.phase_name == "brainstorming":
            hints.append(
                "Brainstorming is underway. You can reference AI-generated seed ideas if available. "
                "Encourage creative thinking and remind participants of the brainstorming norms. "
                "Avoid critiquing ideas during this phase - focus on quantity and divergent thinking."
            )
        
        elif current.phase_name == "clustering_voting":
            hints.append(
                "Ideas have been clustered into themes. You can reference cluster names, "
                "descriptions, and vote counts from the current phase context. When asked about "
                "clusters, you can answer directly from the phase payload showing cluster names "
                "and their vote totals. For detailed cluster membership, use the workshop.list_clusters tool."
            )
        
        elif current.phase_name == "results_feasibility":
            hints.append(
                "Feasibility analysis has been completed. You have access to feasibility rubrics "
                "and cluster evaluations with scores. You can discuss feasibility assessments, "
                "explain scoring criteria, and help interpret the feasibility report that was generated."
            )
        
        elif current.phase_name == "results_prioritization":
            hints.append(
                "Prioritization is complete. You can reference the shortlist of prioritized clusters "
                "positioned on the Impact/Effort matrix. Explain the rationale for prioritization "
                "decisions and help participants understand why certain clusters were recommended."
            )
        
        elif current.phase_name == "discussion":
            hints.append(
                "Discussion phase is active. Help facilitate structured conversation around the "
                "prioritized clusters. Reference previous phases (feasibility scores, vote counts) "
                "to ground the discussion in workshop data.\n"
                "IMPORTANT: There is currently NO tool to add comments or notes to the discussion forum programmatically. "
                "If users want to add their points to the forum, guide them to use the discussion forum UI directly. "
                "You can help them formulate their thoughts and suggest what they might want to post, "
                "but you cannot post on their behalf."
            )
        
        elif current.phase_name == "results_action_plan":
            hints.append(
                "Action plan has been created. You have access to milestones, action items with "
                "assignments, timelines, and dependencies. You can help clarify responsibilities, "
                "explain the roadmap, and answer questions about who is doing what and when."
            )
        
        elif current.phase_name == "summary":
            hints.append(
                "Workshop summary phase. You can reference the complete summary text and key "
                "outcomes. Help participants understand the full arc of the workshop from problem "
                "definition through action planning."
            )
        
        # Add general hint about previous phases
        if context.phase_bundle.previous_phases:
            phase_count = len(context.phase_bundle.previous_phases)
            hints.append(
                f"\nYou have access to summaries and key artifacts from {phase_count} completed "
                f"phase{'s' if phase_count != 1 else ''} in the phase context above. Reference "
                f"these when users ask about earlier workshop activities."
            )
        
        # Add hint about next phase
        if context.phase_bundle.next_phase:
            next_phase = context.phase_bundle.next_phase
            hints.append(
                f"\nNext phase will be: {next_phase.phase_label}. "
                f"If asked what's coming next, you can preview the upcoming phase."
            )
        
        return "\n".join(hints) + "\n"


class PromptBuilder:
    @staticmethod
    def compact_context(ctx: AssistantContext) -> str:
        parts: List[str] = []
        # Current user identity (for personalization)
        try:
            current_uid = getattr(getattr(ctx, "rbac", None), "user_id", None)
        except Exception:
            current_uid = None
        current_user_label = None
        current_participant_id = None
        if current_uid is not None:
            try:
                match = next((p for p in ctx.participants if p.user_id == current_uid), None)
                if match:
                    current_user_label = f"{match.display_name} (role={match.role})"
                    current_participant_id = match.id  # Store participant ID for voting
                else:
                    # Fallback to role from RBAC if participant record not present
                    role = getattr(getattr(ctx, "rbac", None), "role", None)
                    current_user_label = f"user-{current_uid}{f' (role={role})' if role else ''}"
            except Exception:
                current_user_label = f"user-{current_uid}"
        if current_user_label:
            if current_participant_id:
                parts.append(f"You are chatting with: {current_user_label}, Participant ID: {current_participant_id}")
            else:
                parts.append(f"You are chatting with: {current_user_label}")
        # Get enhanced phase name from temporal context if available
        temporal = getattr(ctx, "temporal", {})
        schedule = temporal.get("workshop_schedule", {}) if isinstance(temporal, dict) else {}
        current_phase_name = schedule.get("phase_title") if isinstance(schedule, dict) else None
        
        if current_phase_name:
            parts.append(f"Workshop ID: {ctx.workshop.id}")
            parts.append(f"Workshop: {ctx.workshop.title} (status={ctx.workshop.status}, current phase: {current_phase_name})")
        else:
            parts.append(f"Workshop ID: {ctx.workshop.id}")
            parts.append(f"Workshop: {ctx.workshop.title} (status={ctx.workshop.status}, phase={ctx.workshop.current_phase})")
        if ctx.participants:
            names = ", ".join(p.display_name for p in ctx.participants[:6])
            parts.append(f"Participants: {names}{'…' if len(ctx.participants) > 6 else ''}")
        if ctx.decisions:
            decisions = "; ".join(d.topic for d in ctx.decisions[:3])
            parts.append(f"Recent decisions: {decisions}")
        if ctx.action_items:
            items = ", ".join(a.title for a in ctx.action_items[:3])
            parts.append(f"Open action items: {items}")
        if ctx.snapshots.framing:
            key_question = ctx.snapshots.framing.get("key_question")
            if key_question:
                parts.append(f"Key question: {key_question}")
        temporal = getattr(ctx, "temporal", {}) or {}
        if temporal:
            parts.append(
                f"Local time: {temporal.get('current_time_local', 'unknown')} ({temporal.get('local_timezone', 'UTC')})"
            )
            schedule = temporal.get("workshop_schedule") if isinstance(temporal, dict) else {}
            if isinstance(schedule, dict) and schedule:
                remaining = schedule.get("remaining_minutes_in_phase")
                elapsed = schedule.get("elapsed_minutes")
                phase_title = schedule.get("phase_title")
                phase_description = schedule.get("phase_description")
                
                # Add detailed phase information
                if phase_title:
                    parts.append(f"Current Phase: {phase_title}")
                if phase_description:
                    parts.append(f"Phase Objective: {phase_description}")
                
                # Add timing information
                elements = []
                if isinstance(remaining, int):
                    elements.append(f"remaining={remaining}m")
                if isinstance(elapsed, int):
                    elements.append(f"elapsed={elapsed}m")
                if elements:
                    parts.append("Phase timing: " + ", ".join(elements))
        if getattr(ctx, "time_alerts", None):
            alerts = "; ".join(ctx.time_alerts)
            parts.append(f"Time alerts: {alerts}")
        
        # NEW: Inject phase context
        if ctx.phase_bundle:
            phase_context = PromptBuilder._format_phase_context(ctx.phase_bundle)
            if phase_context:
                parts.append("\n" + phase_context)
        
        if ctx.memory_snippets:
            memory_lines: List[str] = []
            for snippet in ctx.memory_snippets[:3]:
                if not snippet or not snippet.text:
                    continue
                label = snippet.namespace.rsplit("/", 1)[-1] if snippet.namespace else "memory"
                text = snippet.text.strip()
                if len(text) > 220:
                    text = f"{text[:217]}…"
                memory_lines.append(f"- ({label}) {text}")
            if memory_lines:
                parts.append("Memory:")
                parts.extend(memory_lines)
        return "\n".join(parts)
    
    @staticmethod
    def _format_phase_context(bundle) -> str:  # type: ignore
        """Format phase context bundle for LLM consumption.
        
        Args:
            bundle: PhaseContextBundle with previous/current/next contexts
            
        Returns:
            Formatted multi-line string for LLM prompt injection
        """
        lines: List[str] = []
        
        lines.append("=== WORKSHOP PHASE CONTEXT ===")
        lines.append(f"Progress: Phase {bundle.current_phase_index + 1}/{bundle.total_phases}")
        
        # Previous phases summary
        if bundle.previous_phases:
            lines.append("\n**Completed Phases:**")
            for prev in bundle.previous_phases:
                lines.append(f"\n• {prev.phase_label}:")
                lines.append(f"  {prev.summary}")
                
                # Key artifacts (compact)
                if prev.key_artifacts:
                    lines.append("  Key artifacts:")
                    for key, value in prev.key_artifacts.items():
                        if isinstance(value, str) and value:
                            val_str = value[:80] + "..." if len(value) > 80 else value
                            lines.append(f"    - {key}: {val_str}")
                        elif isinstance(value, list) and value:
                            lines.append(f"    - {key}: ({len(value)} items)")
                        elif isinstance(value, dict) and value:
                            # For nested dicts like top_cluster
                            summary_parts = []
                            for k, v in list(value.items())[:2]:
                                summary_parts.append(f"{k}={v}")
                            lines.append(f"    - {key}: {', '.join(summary_parts)}")
                
                # Documents
                if prev.documents:
                    for doc in prev.documents[:2]:  # Limit to 2 docs per phase
                        lines.append(f"  📄 Document ID {doc.id}: {doc.title} ({doc.url})")
        
        # Current phase detail
        if bundle.current_phase:
            curr = bundle.current_phase
            lines.append(f"\n**Current Phase: {curr.phase_label}**")
            lines.append(f"Status: {curr.status.value}")
            
            # Extract important fields from full_payload
            important_fields = PromptBuilder._get_important_fields(curr.phase_name)
            for field in important_fields:
                if field in curr.full_payload and curr.full_payload[field]:
                    value = curr.full_payload[field]
                    formatted = PromptBuilder._format_payload_value(field, value)
                    if formatted:
                        lines.append(f"  {field}: {formatted}")
            
            # Documents
            if curr.documents:
                lines.append("  Documents:")
                for doc in curr.documents[:3]:  # Limit to 3 docs
                    lines.append(f"    📄 Document ID {doc.id}: {doc.title} ({doc.url})")
        
        # Next phase preview
        if bundle.next_phase:
            nxt = bundle.next_phase
            lines.append(f"\n**Next Phase: {nxt.phase_label}**")
            if nxt.description:
                lines.append(f"  {nxt.description}")
        
        return "\n".join(lines)
    
    @staticmethod
    def _get_important_fields(phase_name: str) -> List[str]:
        """Get important fields to extract from phase payload."""
        field_map = {
            "framing": ["problem_statement", "success_criteria", "assumptions", "constraints"],
            "warm-up": ["selected_option", "energy_level", "warm_up_instructions"],
            "warm_up": ["selected_option", "energy_level", "warm_up_instructions"],
            "brainstorming": ["ai_seed_ideas", "participation_norms"],
            "clustering_voting": ["clusters", "corrected", "duplicates"],
            "results_feasibility": ["feasibility_rules_and_rubrics", "cluster_evaluations"],
            "results_prioritization": ["shortlist", "prioritization_rationale"],
            "results_action_plan": ["milestones", "action_items_list"],
            "discussion": ["discussion_prompt", "mode"],
            "summary": ["summary_text", "key_outcomes"],
        }
        return field_map.get(phase_name, [])
    
    @staticmethod
    def _format_payload_value(field: str, value: Any) -> Optional[str]:
        """Format a payload value for display in phase context."""
        if isinstance(value, str):
            # Truncate long strings
            if len(value) > 200:
                return value[:197] + "..."
            return value
        
        elif isinstance(value, list):
            if not value:
                return None
            
            # Special handling for clusters
            if field == "clusters":
                cluster_summaries = []
                for cluster in value[:3]:  # Limit to first 3 clusters
                    if isinstance(cluster, dict):
                        name = cluster.get("name", "Unnamed")
                        votes = cluster.get("votes", 0)
                        cluster_summaries.append(f"{name} ({votes} votes)")
                if cluster_summaries:
                    result = "; ".join(cluster_summaries)
                    if len(value) > 3:
                        result += f" ... and {len(value) - 3} more"
                    return result
            
            # Special handling for success criteria, assumptions, constraints
            elif field in ("success_criteria", "assumptions", "constraints"):
                if all(isinstance(item, str) for item in value):
                    items = [str(item)[:60] + "..." if len(str(item)) > 60 else str(item) for item in value[:3]]
                    result = "; ".join(items)
                    if len(value) > 3:
                        result += f" ... and {len(value) - 3} more"
                    return result
            
            # Generic list handling
            return f"({len(value)} items)"
        
        elif isinstance(value, dict):
            # Show first few keys
            keys = list(value.keys())[:3]
            if keys:
                return f"{{{', '.join(keys)}...}}"
        
        return str(value) if value else None


class AssistantController:
    def __init__(self) -> None:
        self.context_fabric = ContextFabric()
        self.persona_router = PersonaRouter()
        self.tool_executor = ToolExecutor(TOOL_REGISTRY)
        self.tool_registry = build_default_registry()
        self.tool_gateway = ToolGateway(self.tool_registry)
        self.llm = AssistantLLMClient()
        self.memory_settings = AgentCoreMemorySettings.from_app()
        memory_service = AgentMemoryService(self.memory_settings)
        if not memory_service.enabled:
            memory_service = NullMemoryService("memory_disabled")
        self.memory = memory_service

    @staticmethod
    def _prepare_citations(citations: List[AssistantCitationPayload]) -> List[AssistantCitationPayload]:
        if not citations:
            return []

        def _clean(value: Optional[str]) -> Optional[str]:
            if value is None:
                return None
            value = str(value).strip()
            return value or None

        doc_ids: Set[int] = set()
        for citation in citations:
            doc_id = getattr(citation, "document_id", None)
            if doc_id is None or doc_id == "":
                continue
            try:
                doc_ids.add(int(doc_id))
            except (TypeError, ValueError):
                continue

        documents: Dict[int, Document] = {}
        if doc_ids:
            rows = Document.query.filter(Document.id.in_(doc_ids)).all()
            documents = {row.id: row for row in rows}

        cleaned: List[AssistantCitationPayload] = []
        seen_keys: Set[Tuple[Optional[int], Optional[str]]] = set()
        for citation in citations:
            label = _clean(getattr(citation, "display_label", None))
            source_ref = _clean(getattr(citation, "source_ref", None))
            doc_id_raw = getattr(citation, "document_id", None)
            doc_id: Optional[int] = None
            if doc_id_raw not in (None, ""):
                try:
                    doc_id = int(doc_id_raw)
                except (TypeError, ValueError):
                    doc_id = None

            if doc_id and doc_id in documents:
                doc = documents[doc_id]
                title = (doc.title or doc.description or "").strip()
                if title:
                    if not label:
                        citation.display_label = title
                        label = title
                    if not source_ref:
                        citation.source_ref = title
                        source_ref = title
                citation.document_id = doc_id
            elif doc_id and not label and not source_ref:
                fallback = f"Document {doc_id}"
                citation.display_label = fallback
                label = fallback
                citation.document_id = doc_id

            if not label and source_ref:
                citation.display_label = source_ref
                label = source_ref
            elif not source_ref and label:
                citation.source_ref = label
                source_ref = label

            has_meaningful_data = bool(label or source_ref or doc_id)
            if not has_meaningful_data:
                continue

            key = (doc_id, label or source_ref)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            cleaned.append(citation)

        return cleaned

    def handle_query(
        self,
        payload: AssistantQuery,
        *,
        context: AssistantContext | None = None,
        persona: PersonaConfig | None = None,
        thread_id: int | None = None,
    ) -> Tuple[AssistantReply, Dict[str, Any]]:
        context = context or self.context_fabric.build(payload.workshop_id, payload.user_id)
        # Expose only end-user callable tools to the LLM. Internal services like
        # notifier.notify are invoked indirectly via gateway metadata and should
        # not appear in the available tools list to avoid misuse.
        context.available_tools = [
            {
                "name": schema.full_name,
                "description": schema.description,
                "requires_workshop": schema.requires_workshop,
            }
            for schema in self.tool_registry.list_tools()
            if getattr(schema, "namespace", None) != "notifier"
        ]
        persona_cfg = persona or self.persona_router.select(payload, context)
        memory_info = self.memory.retrieve(
            query=payload.text,
            workshop_id=payload.workshop_id,
            user_id=payload.user_id,
            thread_id=thread_id,
        )
        if memory_info.snippets:
            max_snippets = max(1, min(len(memory_info.snippets), 3))
            context.memory_snippets = memory_info.snippets[:max_snippets]
        plan = self.llm.plan(persona_cfg.name, context, payload)
        tool_calls = plan.tool_calls or []

        legacy_calls: List[AssistantToolCall] = []
        gateway_calls: List[AssistantToolCall] = []
        for call in tool_calls:
            if "." in call.name:
                gateway_calls.append(call)
            else:
                legacy_calls.append(call)

        tool_results: List[Dict[str, Any]] = []
        if legacy_calls:
            legacy_results = self.tool_executor.execute(legacy_calls, context)
            tool_results.extend(result.model_dump() for result in legacy_results)
            if any(result.error for result in legacy_results):
                current_app.logger.info("assistant_tool_results", extra={"tool_results": tool_results})

        gateway_meta: List[Dict[str, Any]] = []
        if gateway_calls:
            gateway_results, gateway_raw = self.tool_gateway.execute(
                gateway_calls,
                context,
                correlation_id=str(uuid.uuid4()),
            )
            tool_results.extend(result.model_dump() for result in gateway_results)
            gateway_meta = [result.model_dump() for result in gateway_raw]
        else:
            gateway_raw = []

        # Check if any tool result requests narration suppression
        suppress_narration = any(
            result.metadata.get("suppress_narration", False) 
            for result in gateway_raw 
            if hasattr(result, 'metadata') and result.metadata
        )

        reply = self.llm.compose(persona_cfg.name, context, payload, plan, tool_results)
        if (not reply.ui_hints) and getattr(plan, "ui_hints", None):
            try:
                reply.ui_hints = dict(plan.ui_hints)
            except Exception:
                reply.ui_hints = plan.ui_hints  # fall back to original mapping
        
        # Do not append server-side lists; keep content strictly from the LLM

        # Do not append server-side lists; keep content strictly from the LLM

        # Proactive UX: if list_reports returned a feasibility report, suggest a quick action button
        try:
            if current_app.config.get("ASSISTANT_UI_STRICT_LLM_ONLY", False):
                raise RuntimeError("ui_hints_suppressed")
            if isinstance(reply.ui_hints, dict):
                buttons = []
                # Preserve existing buttons if present
                existing = reply.ui_hints.get("action_buttons") or reply.ui_hints.get("buttons") or reply.ui_hints.get("actions")
                if isinstance(existing, list):
                    buttons.extend(existing)

                # Look for feasibility report URL from tool outputs
                feas_url = None
                for item in tool_results:
                    if not isinstance(item, dict):
                        continue
                    if item.get("name") != "workshop.list_reports":
                        continue
                    data = item.get("output") or item.get("data") or {}
                    reports = data.get("reports") if isinstance(data, dict) else None
                    if not isinstance(reports, list):
                        continue
                    for rep in reports:
                        if isinstance(rep, dict) and rep.get("phase") == "feasibility" and rep.get("url"):
                            feas_url = rep.get("url")
                            break
                    if feas_url:
                        break

                if feas_url:
                    # Avoid duplicates if a similar button already exists
                    has_existing = any(
                        isinstance(btn, dict)
                        and (btn.get("action") == feas_url or btn.get("label", "").lower().startswith("open feasibility report"))
                        for btn in buttons
                    )
                    if not has_existing:
                        buttons.append(
                            {
                                "label": "Open Feasibility Report",
                                "action": feas_url,
                                "variant": "secondary",
                                "tooltip": "Open the most recent feasibility report in a new tab",
                                "icon": "bi-filetype-pdf",
                            }
                        )

                if buttons:
                    reply.ui_hints["action_buttons"] = buttons
        except Exception:
            # Non-critical enhancement; ignore errors
            pass

        # Apply narration suppression if requested by tools
        if suppress_narration:
            reply.speech = None  # Clear speech to prevent TTS
            # Also signal the UI to suppress autoplay/playback for this reply
            try:
                if not getattr(reply, 'ui_hints', None):
                    reply.ui_hints = {}
                # Use a clear flag the frontend can check
                if isinstance(reply.ui_hints, dict):
                    reply.ui_hints['suppress_narration'] = True
            except Exception:
                # Non-fatal UI hint population failure should not break the reply
                pass
        
        reply.citations = self._prepare_citations(list(reply.citations))
        meta = {
            "persona": persona_cfg.name.value,
            "persona_label": persona_cfg.description,
            "tool_results": tool_results,
            "plan": plan.model_dump(),
            "memory": memory_info.as_meta(),
        }
        if gateway_meta:
            meta["tool_gateway"] = gateway_meta
        print(
            "\n\n\n\nassistant_reply_payload",
            {
                "reply": reply.model_dump(),
                "meta": meta,
            },
        )
        return reply, meta

    def persist_turns(
        self,
        payload: AssistantQuery,
        reply: AssistantReply,
        meta: Dict[str, Any],
        thread_id: int,
    ) -> ChatTurn:
        user_turn = ChatTurn(
            thread_id=thread_id,
            workshop_id=payload.workshop_id,
            role="user",
            content=payload.text,
            user_id=payload.user_id,
        )
        db.session.add(user_turn)
        composed_json = reply.model_dump_json()
        assistant_turn = ChatTurn(
            thread_id=thread_id,
            workshop_id=payload.workshop_id,
            role="assistant",
            persona=reply.persona.value if isinstance(reply.persona, PersonaType) else reply.persona,
            content=reply.text,
            json_payload=composed_json,
            composed_json=composed_json,
            plan_json=json.dumps(meta.get("plan"), ensure_ascii=False) if meta.get("plan") else None,
            tool_count=len(reply.tool_calls),
        )
        db.session.add(assistant_turn)
        db.session.flush()
        for citation in reply.citations:
            db.session.add(
                AssistantCitation(
                    turn_id=assistant_turn.id,
                    document_id=citation.document_id,
                    source_type=citation.source_type,
                    source_ref=citation.source_ref,
                    snippet_hash=citation.snippet_hash,
                    start_char=citation.start_char,
                    end_char=citation.end_char,
                )
            )
        db.session.flush()
        return assistant_turn

    def record_memory_event(
        self,
        payload: AssistantQuery,
        reply: AssistantReply,
        thread_id: int,
        meta: Dict[str, Any],
    ) -> None:
        if not getattr(self.memory, "enabled", False):
            return

        user_text = (payload.text or "").strip()
        assistant_text = (reply.text or "").strip()
        if not user_text and not assistant_text:
            return

        memory_metadata: Dict[str, Any] = {}
        persona_value = reply.persona.value if hasattr(reply.persona, "value") else reply.persona
        if persona_value:
            memory_metadata["persona"] = persona_value
        memory_payload = meta.get("memory") if isinstance(meta, dict) else None
        if memory_payload:
            memory_metadata["memory_context"] = memory_payload
        tool_meta = meta.get("tool_results") if isinstance(meta, dict) else None
        if isinstance(tool_meta, list) and tool_meta:
            summary: List[Dict[str, Any]] = []
            for item in tool_meta:
                if not isinstance(item, dict):
                    continue
                summary.append(
                    {
                        "name": item.get("name"),
                        "success": not bool(item.get("error")),
                        "error": item.get("error"),
                    }
                )
            if summary:
                memory_metadata["tool_results"] = summary
        gateway_meta = meta.get("tool_gateway") if isinstance(meta, dict) else None
        if isinstance(gateway_meta, list) and gateway_meta:
            memory_metadata["tool_gateway"] = gateway_meta
        plan_meta = meta.get("plan") if isinstance(meta, dict) else None
        if isinstance(plan_meta, dict):
            memory_metadata["plan"] = plan_meta

        def _store() -> None:
            self.memory.store(
                user_text=user_text,
                assistant_text=assistant_text,
                workshop_id=payload.workshop_id,
                user_id=payload.user_id,
                thread_id=thread_id,
                metadata=memory_metadata,
            )

        if self.memory_settings.store_in_background:
            try:
                from app.extensions import socketio

                socketio.start_background_task(_store)
            except Exception:  # pragma: no cover - background scheduling failures
                current_app.logger.warning("agent_memory_background_store_failed", exc_info=True)
                _store()
        else:
            _store()

    def ensure_thread(self, payload: AssistantQuery) -> ChatThread:
        return self.get_or_create_thread(payload.workshop_id, payload.user_id, payload.thread_id)

    def get_or_create_thread(
        self,
        workshop_id: int,
        user_id: int | None,
        thread_id: int | None = None,
    ) -> ChatThread:
        if thread_id:
            thread = db.session.get(ChatThread, thread_id)
            # Missing or soft-deleted
            if not thread or getattr(thread, "deleted_at", None):
                raise PermissionError("thread_not_found")
            # Wrong workshop
            if thread.workshop_id != workshop_id:
                raise PermissionError("thread_wrong_workshop")
            # Ownership enforcement (no anonymous access per policy answer)
            if user_id is not None and thread.created_by_id != user_id:
                raise PermissionError("thread_forbidden")
            return thread

        if user_id is not None:
            existing = (
                ChatThread.query
                .filter(ChatThread.workshop_id == workshop_id)
                .filter(ChatThread.created_by_id == user_id)
                .filter(ChatThread.deleted_at.is_(None))
                .order_by(ChatThread.created_at.desc())
                .first()
            )
            if existing:
                return existing

        thread = ChatThread(
            workshop_id=workshop_id,
            created_by_id=user_id,
            title="Assistant Thread",
        )
        db.session.add(thread)
        db.session.flush()
        return thread


controller = AssistantController()


@bp.post("/query")
def assistant_query() -> ResponseReturnValue:
    data = request.get_json(force=True) or {}
    try:
        payload = AssistantQuery.model_validate(data)
    except ValidationError as exc:
        return {"error": exc.errors()}, 400

    # Require authentication and enforce user ownership
    if not current_user.is_authenticated:
        return {"error": "authentication_required"}, 401
    try:
        current_uid = int(current_user.get_id()) if current_user.get_id() is not None else None
    except (TypeError, ValueError):
        current_uid = None
    # Default missing user_id to current user (per policy: no anonymous)
    if getattr(payload, "user_id", None) is None and current_uid is not None:
        try:
            # pydantic v2 supports model_copy/update, but a direct setattr is fine here
            setattr(payload, "user_id", current_uid)
        except Exception:
            pass
    # Enforce that provided user_id matches current user
    if getattr(payload, "user_id", None) is None or payload.user_id != current_uid:
        return {"error": "Forbidden"}, 403

    context = controller.context_fabric.build(payload.workshop_id, payload.user_id)
    persona_cfg = controller.persona_router.select(payload, context)
    try:
        thread = controller.ensure_thread(payload)
    except PermissionError as e:
        code = str(e)
        if code == "thread_forbidden":
            return {"error": "Forbidden"}, 403
        return {"error": "Thread not found"}, 404
    try:
        reply, meta = controller.handle_query(
            payload,
            context=context,
            persona=persona_cfg,
            thread_id=thread.id,
        )
    except ValidationError as exc:
        current_app.logger.exception("assistant_validation_error")
        db.session.rollback()
        return {"error": "LLM response validation failed", "details": exc.errors()}, 500
    except Exception as exc:  # pragma: no cover - runtime safety net
        current_app.logger.exception("assistant_unexpected_error")
        db.session.rollback()
        return {"error": str(exc)}, 500

    assistant_turn = controller.persist_turns(payload, reply, meta, thread.id)
    db.session.commit()
    controller.record_memory_event(payload, reply, thread.id, meta)

    timebox = _timebox_payload(context)
    # Build threads list for the current user if feature enabled
    threads_payload = _sidebar_threads(thread, history=[])
    if current_app.config.get("ASSISTANT_THREADS_ENABLED", True) and payload.user_id is not None:
        user_threads = (
            ChatThread.query
            .filter(ChatThread.workshop_id == payload.workshop_id)
            .filter(ChatThread.created_by_id == payload.user_id)
            .filter(ChatThread.deleted_at.is_(None))
            .order_by(ChatThread.created_at.desc())
            .all()
        )
        threads_payload = [
            {
                "id": t.id,
                "title": (t.title or "Assistant Thread"),
                "last_author": None,
                "updated_at": None,
            }
            for t in user_threads
        ] or threads_payload

    meta.update(
        {
            "phase_snapshot": _phase_snapshot(context),
            "sidebar": {
                "actions": _sidebar_actions(context),
                "threads": threads_payload,
            },
            "timer": timebox["formatted"],
            "timer_seconds": timebox["remaining_seconds"],
            "timer_total_seconds": timebox["total_seconds"],
            "timebox_active": timebox["active"],
            "timer_paused": timebox["paused"],
            "workshop_status": timebox["workshop_status"],
        }
    )

    return {
        "thread_id": thread.id,
        "reply": reply.model_dump(),
        "meta": meta,
        "turn_id": assistant_turn.id,
    }, 200


@bp.get("/history")
def assistant_history() -> ResponseReturnValue:
    workshop_id = request.args.get("workshop_id", type=int)
    user_id = request.args.get("user_id", type=int)
    thread_id = request.args.get("thread_id", type=int)

    if not workshop_id:
        return {"error": "workshop_id required"}, 400

    # Require authentication and enforce user ownership; default user_id if missing
    if not current_user.is_authenticated:
        return {"error": "authentication_required"}, 401
    try:
        current_uid = int(current_user.get_id()) if current_user.get_id() is not None else None
    except (TypeError, ValueError):
        current_uid = None
    if user_id is None and current_uid is not None:
        user_id = current_uid
    if user_id is None or current_uid != user_id:
        return {"error": "Forbidden"}, 403

    context = controller.context_fabric.build(workshop_id, user_id)
    seed_query = AssistantQuery(workshop_id=workshop_id, user_id=user_id, text="status")
    persona_cfg = controller.persona_router.select(seed_query, context)
    try:
        thread = controller.get_or_create_thread(workshop_id, user_id, thread_id)
        # Important: if a new thread was created for this user during history load,
        # make sure it's persisted so subsequent socket/query requests can find it.
        try:
            db.session.commit()
        except Exception:
            current_app.logger.exception("assistant_history_commit_failed")
            db.session.rollback()
            # If commit fails, continue returning history without crashing; client will recover.
    except PermissionError as e:
        code = str(e)
        if code == "thread_forbidden":
            return {"error": "Forbidden"}, 403
        return {"error": "Thread not found"}, 404

    turns = (
        ChatTurn.query
        .filter(ChatTurn.thread_id == thread.id)
        .order_by(ChatTurn.created_at.asc())
        .all()
    )

    history: List[Dict[str, Any]] = []
    for turn in turns:
        payload = None
        if turn.json_payload:
            try:
                payload = json.loads(turn.json_payload)
            except Exception:
                payload = None
        history.append(
            {
                "id": turn.id,
                "role": turn.role,
                "persona": turn.persona,
                "content": turn.content,
                "payload": payload,
                "user_id": turn.user_id,
                "created_at": turn.created_at.isoformat() if turn.created_at else None,
            }
        )

    # Build threads list: include user's non-deleted threads if feature enabled and user provided
    threads_payload = _sidebar_threads(thread, history)
    if current_app.config.get("ASSISTANT_THREADS_ENABLED", True) and user_id is not None:
        user_threads = (
            ChatThread.query
            .filter(ChatThread.workshop_id == workshop_id)
            .filter(ChatThread.created_by_id == user_id)
            .filter(ChatThread.deleted_at.is_(None))
            .order_by(ChatThread.created_at.desc())
            .all()
        )
        threads_payload = [
            {
                "id": t.id,
                "title": t.title or "Assistant Thread",
                "last_author": None,
                "updated_at": None,
            }
            for t in user_threads
        ] or threads_payload

    sidebar = {
        "actions": _sidebar_actions(context),
        "threads": threads_payload,
    }

    timebox = _timebox_payload(context)

    return {
        "thread_id": thread.id,
        "messages": history,
        "workshop_title": context.workshop.title,
        "phase": context.workshop.current_phase,
        "timer": timebox["formatted"],
        "timer_seconds": timebox["remaining_seconds"],
        "timer_total_seconds": timebox["total_seconds"],
        "timebox_active": timebox["active"],
        "timer_paused": timebox["paused"],
        "workshop_status": timebox["workshop_status"],
        "rbac": _rbac_payload(context.rbac),
        "persona": persona_cfg.name.value,
        "persona_label": persona_cfg.description,
        "phase_snapshot": _phase_snapshot(context),
        "sidebar": sidebar,
    }, 200


@bp.get("/threads")
def assistant_list_threads() -> ResponseReturnValue:
    if not current_app.config.get("ASSISTANT_THREADS_ENABLED", True):
        return {"threads": []}
    workshop_id = request.args.get("workshop_id", type=int)
    user_id = request.args.get("user_id", type=int)
    if not workshop_id or not user_id:
        return {"error": "workshop_id and user_id required"}, 400
    # Auth + ownership
    if not current_user.is_authenticated:
        return {"error": "authentication_required"}, 401
    try:
        current_uid = int(current_user.get_id()) if current_user.get_id() is not None else None
    except (TypeError, ValueError):
        current_uid = None
    if current_uid != user_id:
        return {"error": "Forbidden"}, 403
    items = (
        ChatThread.query
        .filter(ChatThread.workshop_id == workshop_id)
        .filter(ChatThread.created_by_id == user_id)
        .filter(ChatThread.deleted_at.is_(None))
        .order_by(ChatThread.created_at.desc())
        .all()
    )
    return {"threads": [{"id": t.id, "title": t.title or "Assistant Thread"} for t in items]}


@bp.post("/threads")
def assistant_create_thread() -> ResponseReturnValue:
    if not current_app.config.get("ASSISTANT_THREADS_ENABLED", True):
        return {"error": "disabled"}, 403
    data = request.get_json(force=True) or {}
    workshop_id = data.get("workshop_id")
    user_id = data.get("user_id")
    title = (data.get("title") or "Assistant Thread").strip()[:200]
    if not workshop_id or not user_id:
        return {"error": "workshop_id and user_id required"}, 400
    # Auth + ownership
    if not current_user.is_authenticated:
        return {"error": "authentication_required"}, 401
    try:
        current_uid = int(current_user.get_id()) if current_user.get_id() is not None else None
    except (TypeError, ValueError):
        current_uid = None
    if current_uid != int(user_id):
        return {"error": "Forbidden"}, 403
    t = ChatThread()
    t.workshop_id = workshop_id
    t.created_by_id = int(user_id)
    t.title = title
    db.session.add(t)
    db.session.commit()
    return {"id": t.id, "title": t.title}, 201


@bp.patch("/threads/<int:thread_id>")
def assistant_rename_thread(thread_id: int) -> ResponseReturnValue:
    if not current_app.config.get("ASSISTANT_THREADS_ENABLED", True):
        return {"error": "disabled"}, 403
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()[:200]
    if not title:
        return {"error": "title required"}, 400
    user_id = data.get("user_id") or request.args.get("user_id", type=int)
    # Auth + ownership
    if not current_user.is_authenticated:
        return {"error": "authentication_required"}, 401
    try:
        current_uid = int(current_user.get_id()) if current_user.get_id() is not None else None
    except (TypeError, ValueError):
        current_uid = None
    t = db.session.get(ChatThread, thread_id)
    if not t or getattr(t, "deleted_at", None):
        return {"error": "Thread not found"}, 404
    if user_id is None or t.created_by_id != int(user_id) or current_uid != int(user_id):
        return {"error": "Forbidden"}, 403
    t.title = title
    db.session.commit()
    return {"id": t.id, "title": t.title}


@bp.delete("/threads/<int:thread_id>")
def assistant_delete_thread(thread_id: int) -> ResponseReturnValue:
    if not current_app.config.get("ASSISTANT_THREADS_ENABLED", True):
        return {"error": "disabled"}, 403
    # Auth + ownership
    if not current_user.is_authenticated:
        return {"error": "authentication_required"}, 401
    user_id = request.args.get("user_id", type=int)
    try:
        current_uid = int(current_user.get_id()) if current_user.get_id() is not None else None
    except (TypeError, ValueError):
        current_uid = None
    t = db.session.get(ChatThread, thread_id)
    if not t or getattr(t, "deleted_at", None):
        return {"error": "Thread not found"}, 404
    if user_id is None or t.created_by_id != int(user_id) or current_uid != int(user_id):
        return {"error": "Forbidden"}, 403
    from datetime import datetime as _dt
    t.deleted_at = _dt.utcnow()
    db.session.commit()
    return {"id": t.id, "deleted": True}


def _format_timer(timer: TimerSnapshot | None) -> str:
    if not timer or timer.remaining_seconds is None:
        return "—"
    remaining = max(int(timer.remaining_seconds), 0)
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _rbac_payload(rbac: RBACContext | None) -> Dict[str, Any]:
    if not rbac:
        return {"role": "guest", "is_facilitator": False}
    return {
        "role": rbac.role or "participant",
        "is_facilitator": bool(rbac.is_facilitator),
    }


def _timebox_payload(context: AssistantContext) -> Dict[str, Any]:
    timer = context.timers
    remaining = getattr(timer, "remaining_seconds", None)
    total = getattr(timer, "total_duration_seconds", None)
    status_raw = getattr(context.workshop, "status", None) or ""
    status = status_raw.lower() if isinstance(status_raw, str) else str(status_raw).lower()
    paused = status == "paused"
    running_statuses = {"inprogress", "running"}
    active = (
        remaining is not None
        and int(remaining) > 0
        and status in running_statuses
    )
    formatted = _format_timer(timer)
    return {
        "active": active,
        "paused": paused,
        "remaining_seconds": remaining if remaining is not None else None,
        "total_seconds": total if total is not None else None,
        "formatted": formatted,
        "workshop_status": status_raw,
    }


PHASE_LABELS = {
    "framing": "Briefing",
    "briefing": "Briefing",
    "warm-up": "Warm-up",
    "warm_up": "Warm-up",
    "warmup": "Warm-up",
    "brainstorming": "Ideas",
    "ideas": "Ideas",
    "clustering_voting": "Clustering",
    "clustering": "Clustering",
    "results_feasibility": "Feasibility",
    "feasibility": "Feasibility",
    "results_prioritization": "Prioritization",
    "prioritization": "Prioritization",
    "results_action_plan": "Action Plan",
    "action_plan": "Action Plan",
    "discussion": "Discussion",
    "summary": "Summary",
}


def _friendly_phase_name(raw: str | None) -> Optional[str]:
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered in PHASE_LABELS:
        return PHASE_LABELS[lowered]
    simplified = lowered.replace(" ", "_").replace("-", "_")
    if simplified in PHASE_LABELS:
        return PHASE_LABELS[simplified]
    if ":" in candidate:
        prefix = candidate.split(":", 1)[0].strip()
        pref_key = prefix.lower().replace(" ", "_").replace("-", "_")
        if pref_key in PHASE_LABELS:
            return PHASE_LABELS[pref_key]
        return prefix or candidate
    return candidate


def _phase_snapshot(context: AssistantContext) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    timebox = _timebox_payload(context)
    phase_label = _friendly_phase_name(getattr(context.workshop, "current_phase", None))

    if phase_label:
        items.append({
            "label": "Current phase",
            "value": phase_label,
            "field": "phase",
        })
        
   # if context.workshop.current_task_title:
   #     items.append({
   #         "label": "Active task",
   #         "value": context.workshop.current_task_title,
   #         "field": "active_task",
   #     })
    if timebox.get("remaining_seconds") is not None:
        items.append(
            {
                "label": "Time remaining",
                "value": timebox["formatted"],
                "field": "time_remaining",
                "remaining_seconds": timebox["remaining_seconds"],
            }
        )
    if timebox.get("total_seconds"):
        total_minutes = int(timebox["total_seconds"] // 60)
        items.append(
            {
                "label": "Timebox",
                "value": f"{total_minutes} min",
                "field": "timebox",
            }
        )
    return items


def _sidebar_actions(context: AssistantContext) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for action in context.action_items[:5]:
        due_date = None
        if action.due_date:
            try:
                due_date = action.due_date.isoformat()
            except AttributeError:  # pragma: no cover - defensive for legacy data
                due_date = str(action.due_date)
        actions.append(
            {
                "title": action.title,
                "summary": action.description or action.success_metric or action.status or "",
                "description": action.description or "",
                "status": action.status or "",
                "type": action.status or "",
                "due_date": due_date,
            }
        )
    return actions


def _sidebar_threads(thread: ChatThread, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    last_entry = history[-1] if history else None
    last_author = None
    if last_entry:
        if last_entry.get("role") == "assistant":
            last_author = last_entry.get("persona") or "Assistant"
        else:
            last_author = "Participant"
    return [
        {
            "id": thread.id,
            "title": thread.title or "Assistant Thread",
            "last_author": last_author,
            "updated_at": last_entry.get("created_at") if last_entry else None,
        }
    ]


@bp.post("/feedback")
def assistant_feedback() -> ResponseReturnValue:
    if not current_user.is_authenticated:
        return {"error": "authentication_required"}, 401
    data = request.get_json(force=True) or {}
    try:
        payload = AssistantFeedbackPayload.model_validate(data)
    except ValidationError as exc:
        return {"error": exc.errors()}, 400

    turn = db.session.get(ChatTurn, payload.turn_id)
    if not turn:
        return {"error": "turn_not_found"}, 404

    user_id = current_user.get_id()
    try:
        user_id_int = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id_int = None

    feedback = (
        db.session.query(AssistantMessageFeedback)
        .filter(AssistantMessageFeedback.turn_id == payload.turn_id, AssistantMessageFeedback.user_id == user_id_int)
        .one_or_none()
    )

    if feedback:
        feedback.rating = payload.rating
        feedback.comment = payload.comment
    else:
        feedback = AssistantMessageFeedback()
        feedback.turn_id = payload.turn_id
        feedback.workshop_id = turn.workshop_id
        feedback.user_id = user_id_int
        feedback.rating = payload.rating
        feedback.comment = payload.comment
        db.session.add(feedback)

    db.session.commit()
    return {"status": "ok", "rating": feedback.rating}
