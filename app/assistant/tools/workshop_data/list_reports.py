from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import os
from flask import current_app

from app.assistant.tools.base import BaseTool
from app.assistant.tools.types import ToolResult, ToolSchema
from app.config import Config
from app.models import Document, Workshop, WorkshopDocument, db


def _classify_phase(title: str, desc: str, fname: str) -> str:
    text = f"{title} {desc} {fname}".lower()
    if any(k in text for k in ["framing brief", "framing", "workshop briefing", "briefing"]):
        return "framing"
    if "feasibility" in text:
        return "feasibility"
    if any(k in text for k in ["shortlist", "prioritization", "prioritisation"]):
        return "prioritization"
    if any(k in text for k in ["action plan", "action_plan"]):
        return "action_plan"
    if "summary" in text:
        return "summary"
    return "unknown"


def _to_url(file_path: str | None) -> Optional[str]:
    if not file_path:
        return None
    # Expect file_path like "uploads/reports/<name>.pdf"
    try:
        base = os.path.basename(file_path)
        return f"{Config.MEDIA_REPORTS_URL_PREFIX}/{base}"
    except Exception:
        return None


class ListReportsTool(BaseTool):
    """List generated workshop reports and briefs with URLs and metadata.

    Sources: Documents linked to the workshop via WorkshopDocument.
    Phases recognized: framing, feasibility, prioritization, action_plan, summary.
    """

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_reports",
            namespace="workshop",
            description=(
                "List the latest generated reports for this workshop (framing brief, feasibility report, "
                "prioritization shortlist, action plan, summary). Returns URLs and metadata."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    # Gateway auto-injects user_id; accept it to satisfy strict schema validation
                    "user_id": {"type": "integer", "minimum": 1},
                    "phase": {
                        "type": "string",
                        "description": "Optional phase filter; accepts synonyms like 'framing brief', 'action plan'; defaults to 'any'",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": [],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "reports": {"type": "array"},
                    "count": {"type": "integer"},
                },
            },
            requires_auth=True,
            requires_workshop=True,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop_id = params.get("workshop_id")
        self.ensure_workshop(workshop_id)

        # Normalize phase strings and allow synonyms
        phase_raw = (params.get("phase") or "any").strip().lower()
        alias_map = {
            "any": "any",
            "framing": "framing",
            "framing brief": "framing",
            "framing_brief": "framing",
            "brief": "framing",
            "briefing": "framing",
            "workshop briefing": "framing",
            "workshop_briefing": "framing",
            "feasibility": "feasibility",
            "feasibility report": "feasibility",
            "feasibility_report": "feasibility",
            "prioritization": "prioritization",
            "prioritisation": "prioritization",
            "shortlist": "prioritization",
            "shortlisting": "prioritization",
            "action plan": "action_plan",
            "action_plan": "action_plan",
            "plan": "action_plan",
            "summary": "summary",
        }
        phase_filter = alias_map.get(phase_raw, phase_raw)
        try:
            limit = int(params.get("limit") or 10)
        except Exception:
            limit = 10
        limit = max(1, min(limit, 50))

        # Join WorkshopDocument -> Document; newest first
        links = (
            WorkshopDocument.query
            .filter_by(workshop_id=workshop_id)
            .order_by(WorkshopDocument.added_at.desc())
            .limit(limit if phase_filter == "any" else 100)  # pull extra for filtering
            .all()
        )

        results: List[Dict[str, Any]] = []
        for link in links:
            doc: Optional[Document] = None
            try:
                doc = db.session.get(Document, link.document_id)
            except Exception:
                doc = None
            if not doc:
                continue
            title = (doc.title or "").strip()
            desc = (doc.description or "").strip()
            fname = (doc.file_name or "").strip()
            url = _to_url(doc.file_path)
            phase = _classify_phase(title, desc, fname)
            if phase_filter != "any" and phase != phase_filter:
                continue
            uploaded_at = getattr(doc, "uploaded_at", None)
            results.append(
                {
                    "document_id": doc.id,
                    "title": title or fname or f"Document #{doc.id}",
                    "phase": phase,
                    "url": url,
                    "file_name": fname,
                    "file_size": getattr(doc, "file_size", None),
                    "uploaded_at": uploaded_at.isoformat() if uploaded_at else None,
                    "description": desc,
                }
            )

        # Trim to requested limit after filtering
        if phase_filter != "any":
            results = results[:limit]

        return ToolResult(success=True, data={"reports": results, "count": len(results)})
