from __future__ import annotations

from typing import Any, Dict, Optional

import os
from flask import current_app

from app.assistant.tools.base import BaseTool
from app.assistant.tools.types import ToolResult, ToolSchema
from app.config import Config
from app.models import Document, WorkshopDocument, db
from app.document.service.extractors import extract_content


def _abs_instance_path(rel_path: str) -> str:
    base = current_app.instance_path.rstrip("/")
    rel = rel_path.lstrip("/")
    return os.path.join(base, rel)


def _resolve_document(workshop_id: int, *, document_id: Optional[int] = None, url: Optional[str] = None) -> Optional[Document]:
    doc: Optional[Document] = None
    if document_id:
        doc = db.session.get(Document, int(document_id))
    elif url:
        try:
            # Accept either absolute/relative URL; match by filename
            fname = os.path.basename(url)
            if fname:
                doc = db.session.query(Document).filter(Document.file_name == fname).order_by(Document.id.desc()).first()
        except Exception:
            doc = None

    if not doc:
        return None

    # Enforce that the document is linked to this workshop
    link = (
        db.session.query(WorkshopDocument)
        .filter(WorkshopDocument.workshop_id == workshop_id, WorkshopDocument.document_id == doc.id)
        .first()
    )
    if not link:
        return None
    return doc


class ReadReportTool(BaseTool):
    """Read and return text content from a workshop-linked report/document.

    Prefers stored Document.content; falls back to on-demand extraction using extractors.
    """

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_report",
            namespace="workshop",
            description=(
                "Read the text content of a report/document linked to this workshop. "
                "Use after locating a document via workshop.list_reports."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer", "minimum": 1},
                    # Gateway auto-injects user_id; accept to satisfy strict schema
                    "user_id": {"type": "integer", "minimum": 1},
                    "document_id": {"type": "integer", "minimum": 1},
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 20000},
                },
                "required": ["workshop_id"],
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "text": {"type": "string"},
                    "chars": {"type": "integer"},
                    "pages": {"type": "integer"},
                },
            },
            requires_auth=True,
            requires_workshop=True,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop_id = params.get("workshop_id")
        self.ensure_workshop(workshop_id)

        document_id = params.get("document_id")
        url = params.get("url")
        try:
            max_chars = int(params.get("max_chars") or 20000)
        except Exception:
            max_chars = 20000
        max_chars = max(500, min(max_chars, 200000))

        doc = _resolve_document(int(workshop_id), document_id=document_id, url=url)
        if not doc:
            return ToolResult(success=False, error="Document not found or not linked to workshop")

        # Prefer already stored content
        text: str | None = getattr(doc, "content", None)
        pages: int | None = None
        if not (text and text.strip()):
            # Extract from file
            rel_path = getattr(doc, "file_path", None)
            if not rel_path:
                return ToolResult(success=False, error="Document file path unavailable")
            abs_path = _abs_instance_path(rel_path)
            if not os.path.exists(abs_path):
                return ToolResult(success=False, error="Document file not found on disk")
            try:
                from pathlib import Path
                result = extract_content(Path(abs_path))
            except Exception as exc:
                return ToolResult(success=False, error=f"Extraction failed: {exc}")
            text = (result.content or "").strip()
            pages = getattr(result, "total_pages", None) or getattr(result, "pages", None)

        safe_text = (text or "").strip()
        if not safe_text:
            return ToolResult(success=False, error="No extractable text content")

        if len(safe_text) > max_chars:
            safe_text = safe_text[: max(0, max_chars - 1)] + "\n…"

        url_out = f"{Config.MEDIA_REPORTS_URL_PREFIX}/{os.path.basename(getattr(doc, 'file_name', '') or getattr(doc, 'file_path', '') or '')}".rstrip('/')
        data = {
            "document_id": int(doc.id),
            "title": getattr(doc, "title", "Document"),
            "url": url_out,
            "text": safe_text,
            "chars": len(safe_text),
            "pages": pages,
        }
        return ToolResult(success=True, data=data)
