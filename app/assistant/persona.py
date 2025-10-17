from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from app.assistant.schemas import AssistantQuery, PersonaType
from app.assistant.context import AssistantContext


@dataclass
class PersonaConfig:
    name: PersonaType
    description: str
    primer: str


PERSONA_LIBRARY: Dict[PersonaType, PersonaConfig] = {
    PersonaType.GUIDE: PersonaConfig(
        name=PersonaType.GUIDE,
        description="Primary facilitator responding with grounded guidance.",
        primer="You are the workshop guide. Provide concise, actionable insight with citations when available.",
    ),
    PersonaType.SCRIBE: PersonaConfig(
        name=PersonaType.SCRIBE,
        description="Summarises discussion and captures notes.",
        primer="You are the workshop scribe. Produce crisp summaries and highlight decisions or action items.",
    ),
    PersonaType.MEDIATOR: PersonaConfig(
        name=PersonaType.MEDIATOR,
        description="Helps resolve conflicts and maintain balance.",
        primer="You are the mediator. Maintain neutrality, surface opposing views, and encourage balanced participation.",
    ),
    PersonaType.DEVIL: PersonaConfig(
        name=PersonaType.DEVIL,
        description="Challenges assumptions and stress-tests ideas.",
        primer="You are the devil's advocate. Challenge politely, surface risks, and suggest alternatives without dismissing contributions.",
    ),
    PersonaType.ANALYST: PersonaConfig(
        name=PersonaType.ANALYST,
        description="Explains quantitative or visual insights.",
        primer="You are the analyst. Interpret data, charts, and quantitative signals clearly using grounded numbers.",
    ),
}


class PersonaRouter:
    def __init__(self, default: PersonaType = PersonaType.GUIDE):
        self.default = default

    def select(self, query: AssistantQuery, context: AssistantContext) -> PersonaConfig:
        if query.persona_hint and query.persona_hint in PERSONA_LIBRARY:
            base_config = PERSONA_LIBRARY[query.persona_hint]
            return self._enhance_with_workshop_context(base_config, context)

        lowered = query.text.lower()
        if any(token in lowered for token in ["summarize", "summary", "recap", "scribe"]):
            base_config = PERSONA_LIBRARY[PersonaType.SCRIBE]
        elif any(token in lowered for token in ["conflict", "disagree", "mediator", "resolve"]):
            base_config = PERSONA_LIBRARY[PersonaType.MEDIATOR]
        elif any(token in lowered for token in ["risk", "challenge", "devil"]):
            base_config = PERSONA_LIBRARY[PersonaType.DEVIL]
        elif any(token in lowered for token in ["chart", "graph", "metric", "data", "analysis"]):
            base_config = PERSONA_LIBRARY[PersonaType.ANALYST]
        elif context.rbac and context.rbac.is_facilitator and "broadcast" in lowered:
            base_config = PERSONA_LIBRARY[PersonaType.GUIDE]
        else:
            base_config = PERSONA_LIBRARY[self.default]

        return self._enhance_with_workshop_context(base_config, context)

    def _enhance_with_workshop_context(self, base_config: PersonaConfig, context: AssistantContext) -> PersonaConfig:
        """Enhance persona primer with current workshop phase context."""
        temporal = getattr(context, "temporal", {})
        if not temporal:
            return base_config
            
        schedule = temporal.get("workshop_schedule", {})
        if not isinstance(schedule, dict):
            return base_config
            
        phase_title = schedule.get("phase_title")
        phase_description = schedule.get("phase_description")
        
        if not phase_title or not phase_description:
            return base_config
            
        # Create enhanced primer with phase context
        enhanced_primer = f"{base_config.primer} Currently facilitating: {phase_title}. Phase objective: {phase_description} Provide guidance specific to this workshop phase."
        
        return PersonaConfig(
            name=base_config.name,
            description=f"{base_config.description} (Phase: {phase_title})",
            primer=enhanced_primer
        )

    def get_primer(self, persona: PersonaType) -> Optional[str]:
        cfg = PERSONA_LIBRARY.get(persona)
        return cfg.primer if cfg else None
