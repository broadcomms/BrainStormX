"""Shared document management operations for reuse across blueprints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from flask import current_app
from app.extensions import db
from app.models import (
    Document,
    DocumentAudio,
    DocumentProcessingJob,
    DocumentProcessingLog,
    DocumentProcessingLogArchive,
)


@dataclass(frozen=True)
class DocumentDeleteResult:
    success: bool
    message: str
    category: str


def _collect_document_family(root: Document) -> List[Document]:
    seen: set[int] = set()
    stack: list[Document] = [root]
    ordered: list[Document] = []
    while stack:
        current = stack.pop()
        if current.id in seen:
            continue
        seen.add(current.id)
        ordered.append(current)
        children: Iterable[Document] = list(getattr(current, "child_documents", []) or [])
        stack.extend(children)
    return ordered


def delete_document_tree(document: Document, *, actor_id: Optional[int]) -> DocumentDeleteResult:
    """Delete a document and all of its derived versions plus processing artifacts."""

    documents_to_delete = _collect_document_family(document)
    document_ids = [doc.id for doc in documents_to_delete]
    version_count = max(0, len(documents_to_delete) - 1)
    document_title = document.title

    session = db.session
    base_path = Path(current_app.instance_path)

    document_file_paths: set[Path] = set()
    audio_file_paths: set[Path] = set()
    audio_directories: set[Path] = set()
    doc_workspace_map = {doc_entry.id: doc_entry.workspace_id for doc_entry in documents_to_delete}

    for doc_entry in documents_to_delete:
        if doc_entry.file_path:
            document_file_paths.add(base_path / doc_entry.file_path)

        for audio in list(getattr(doc_entry, "audios", []) or []):
            if audio.audio_file_path:
                audio_path = base_path / audio.audio_file_path
                audio_file_paths.add(audio_path)
                audio_directories.add(audio_path.parent)
            session.delete(audio)

        DocumentProcessingJob.query.filter_by(document_id=doc_entry.id).delete(synchronize_session=False)

    logs_to_archive: List[DocumentProcessingLog] = []
    if document_ids:
        logs_to_archive = (
            DocumentProcessingLog.query.filter(
                DocumentProcessingLog.document_id.in_(document_ids)
            ).all()
        )

    if logs_to_archive:
        archived_at = datetime.utcnow()
        for log in logs_to_archive:
            archive_entry = DocumentProcessingLogArchive()
            archive_entry.document_id = log.document_id
            archive_entry.workspace_id = doc_workspace_map.get(log.document_id)
            archive_entry.stage = log.stage
            archive_entry.status = log.status
            archive_entry.error_message = log.error_message
            archive_entry.processed_pages = log.processed_pages
            archive_entry.total_pages = log.total_pages
            archive_entry.started_at = log.started_at
            archive_entry.completed_at = log.completed_at
            archive_entry.created_at = log.created_at or archived_at
            archive_entry.archived_at = archived_at
            archive_entry.archived_by_id = actor_id
            archive_entry.source_log_id = log.id
            session.add(archive_entry)

        DocumentProcessingLog.query.filter(
            DocumentProcessingLog.document_id.in_(document_ids)
        ).delete(synchronize_session=False)

    try:
        for doc_entry in reversed(documents_to_delete):
            session.delete(doc_entry)
        session.commit()
    except Exception as exc:  # pragma: no cover - safety net
        session.rollback()
        current_app.logger.error(
            "Document delete failed",
            extra={"document_ids": document_ids, "error": str(exc)},
        )
        return DocumentDeleteResult(False, "Failed to delete document. See logs for details.", "danger")

    current_app.logger.info(
        "Document delete committed",
        extra={"document_ids": document_ids, "workspace_ids": list(doc_workspace_map.values())},
    )

    for path in document_file_paths:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            current_app.logger.warning("Unable to delete document file %s: %s", path, exc)

    for audio_path in audio_file_paths:
        try:
            if audio_path.exists():
                audio_path.unlink()
        except OSError as exc:
            current_app.logger.warning("Unable to delete audio file %s: %s", audio_path, exc)

    for directory in sorted(audio_directories, key=lambda item: len(str(item)), reverse=True):
        try:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass

    if version_count:
        message = f'Document "{document_title}" and {version_count} version(s) deleted successfully.'
    else:
        message = f'Document "{document_title}" deleted successfully.'

    return DocumentDeleteResult(True, message, "success")