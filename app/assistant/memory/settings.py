from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from app.config import Config

_DEFAULT_NAMESPACE_TEMPLATES = [
    "support/user/{actorId}/facts",
    "support/user/{actorId}/preferences",
    "support/user/{actorId}/{sessionId}",
]


@dataclass
class AgentCoreMemorySettings:
    enabled: bool
    memory_id: Optional[str]
    region: str
    top_k: int
    timeout_seconds: float
    namespace_templates: List[str] = field(default_factory=list)
    store_in_background: bool = True
    debug_log: bool = False
    memory_arn: Optional[str] = None

    @classmethod
    def from_app(cls) -> "AgentCoreMemorySettings":
        raw_templates = Config.AGENTCORE_MEMORY_NAMESPACE_TEMPLATES
        namespace_templates: List[str]
        if raw_templates:
            try:
                parsed = json.loads(raw_templates)
                if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                    namespace_templates = [item for item in parsed if item]
                else:
                    namespace_templates = _DEFAULT_NAMESPACE_TEMPLATES.copy()
            except json.JSONDecodeError:
                normalised = raw_templates.replace("\n", ",").replace(";", ",")
                namespace_templates = [
                    token.strip()
                    for token in normalised.split(",")
                    if token.strip()
                ]
        else:
            namespace_templates = _DEFAULT_NAMESPACE_TEMPLATES.copy()

        if not namespace_templates:
            namespace_templates = _DEFAULT_NAMESPACE_TEMPLATES.copy()

        enabled = Config.AGENTCORE_MEMORY_ENABLED and bool(Config.AGENTCORE_MEMORY_ID)

        return cls(
            enabled=enabled,
            memory_id=Config.AGENTCORE_MEMORY_ID,
            region=Config.AGENTCORE_MEMORY_REGION,
            memory_arn=Config.AGENTCORE_MEMORY_ARN,
            top_k=Config.AGENTCORE_MEMORY_TOP_K,
            timeout_seconds=Config.AGENTCORE_MEMORY_TIMEOUT_SECONDS,
            namespace_templates=namespace_templates,
            store_in_background=Config.AGENTCORE_MEMORY_STORE_BACKGROUND,
            debug_log=Config.AGENTCORE_MEMORY_DEBUG_LOG,
        )

    def formatted_namespaces(self, actor_id: str, session_id: str, workshop_id: int) -> List[str]:
        placeholders = {
            "actorId": actor_id,
            "actor_id": actor_id,
            "sessionId": session_id,
            "session_id": session_id,
            "workshopId": workshop_id,
            "workshop_id": workshop_id,
        }
        formatted: List[str] = []
        for template in self.namespace_templates:
            try:
                formatted.append(template.format(**placeholders))
            except Exception:
                # Skip malformed templates but continue processing others
                continue
        return formatted
