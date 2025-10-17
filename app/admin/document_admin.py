"""Administrative helper utilities for managing documents."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import Document, Workspace


class DocumentAdmin:
    """Utility helpers supporting the admin document views."""

    @staticmethod
    def workspace_choices() -> List[Tuple[int, str]]:
        """Return select options for all workspaces ordered by name."""

        workspaces: Sequence[Workspace] = Workspace.query.order_by(Workspace.name.asc()).all()
        choices: List[Tuple[int, str]] = []
        for ws in workspaces:
            label = ws.name or f"Workspace {ws.workspace_id}"
            choices.append((int(ws.workspace_id), label))
        return choices

    @staticmethod
    def ensure_upload_directory() -> Path:
        """Ensure the admin document upload directory exists and return it."""

        upload_dir = Path(current_app.instance_path) / "uploads" / "documents"
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_dir

    @staticmethod
    def _normalize_title(raw_title: str | None, filename: str) -> str:
        if raw_title:
            normalized = raw_title.strip()
            if normalized:
                return normalized
        stem = Path(filename).stem
        return stem or "Untitled Document"

    @staticmethod
    def save_upload(
        *,
        form_data: dict[str, str | int | None],
        file_storage: FileStorage | None,
        actor_id: int,
    ) -> Document:
        """Persist an uploaded document initiated from the admin console.

        Args:
            form_data: Mapping extracted from the validated WTForm fields.
            file_storage: Uploaded file reference from the form.
            actor_id: The administrator performing the upload.

        Returns:
            The newly created ``Document`` instance (already committed).

        Raises:
            ValueError: If required information is missing or validation fails.
            RuntimeError: If the file cannot be written or the database commit fails.
        """

        workspace_id = form_data.get("workspace_id")
        if workspace_id is None:
            raise ValueError("A workspace must be selected.")
        try:
            workspace_id_int = int(workspace_id)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive guard
            raise ValueError("Invalid workspace selection.") from exc

        workspace: Workspace | None = Workspace.query.get(workspace_id_int)
        if workspace is None:
            raise ValueError("The selected workspace no longer exists.")

        if file_storage is None or not getattr(file_storage, "filename", ""):
            raise ValueError("A document file must be provided.")

        sanitized_filename = secure_filename(str(file_storage.filename))
        if not sanitized_filename:
            raise ValueError("Unable to determine a safe filename for the upload.")

        title_raw = form_data.get("title")
        title_input = title_raw if isinstance(title_raw, str) else None
        title = DocumentAdmin._normalize_title(title_input, sanitized_filename)

        description_raw = form_data.get("description")
        if isinstance(description_raw, str):
            description_clean = description_raw.strip()
            description_value = description_clean or None
        else:
            description_value = None

        upload_dir = DocumentAdmin.ensure_upload_directory()
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        unique_filename = f"admin_{actor_id}_{workspace_id_int}_{timestamp}_{sanitized_filename}"
        destination = upload_dir / unique_filename

        relative_path = Path("uploads") / "documents" / unique_filename

        try:
            file_storage.save(destination)
            file_size = destination.stat().st_size
        except Exception as exc:  # pragma: no cover - filesystem errors
            if destination.exists():
                try:
                    destination.unlink()
                except OSError:
                    pass
            raise RuntimeError(f"Failed to save uploaded file: {exc}") from exc

        document = Document()
        document.workspace_id = workspace_id_int
        document.title = title
        document.description = description_value
        document.file_name = sanitized_filename
        document.file_path = str(relative_path)
        document.uploaded_by_id = actor_id
        document.file_size = file_size
        document.processing_status = "pending"

        try:
            db.session.add(document)
            db.session.commit()
        except Exception as exc:  # pragma: no cover - database errors
            db.session.rollback()
            try:
                destination.unlink()
            except OSError:
                pass
            raise RuntimeError(f"Failed to persist document: {exc}") from exc

        return document

    @staticmethod
    def summarize_documents(documents: Iterable[Document]) -> dict[str, int]:
        """Compute quick metrics for the admin overview page."""

        total = 0
        archived = 0
        processing = 0
        total_bytes = 0
        for doc in documents:
            total += 1
            total_bytes += int(getattr(doc, "file_size", 0) or 0)
            if getattr(doc, "is_archived", False):
                archived += 1
            if getattr(doc, "processing_status", "").lower() in {"processing", "queued"}:
                processing += 1
        return {
            "count": total,
            "archived": archived,
            "processing": processing,
            "total_bytes": total_bytes,
        }
