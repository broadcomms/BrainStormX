from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class MemorySnippet:
    namespace: str
    text: str
    relevance: Optional[float] = None
    created_at: Optional[datetime] = None
    raw: Optional[Dict[str, object]] = None


@dataclass
class MemoryRetrieval:
    snippets: List[MemorySnippet] = field(default_factory=list)
    namespaces: List[str] = field(default_factory=list)
    latency_ms: Optional[int] = None
    errors: List[str] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "MemoryRetrieval":
        return cls()

    def as_meta(self) -> Dict[str, object]:
        return {
            "count": len(self.snippets),
            "namespaces": sorted({snippet.namespace for snippet in self.snippets}),
            "latency_ms": self.latency_ms,
            "errors": self.errors,
        }
