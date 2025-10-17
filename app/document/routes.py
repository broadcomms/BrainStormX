# app/document/routes.py
from __future__ import annotations

from typing import Any, Mapping, cast

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app,
    abort,
    send_from_directory,
    jsonify,
)
from flask.typing import ResponseReturnValue
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload

from app.models import (
    Document,
    Workspace,
    User,
    WorkspaceMember,
    WorkshopDocument,
    WorkshopParticipant,
    Chunk,
    DocumentProcessingLog,
    DocumentAudio,
    DocumentProcessingJob,
    DocumentProcessingLogArchive,
)
from app.extensions import db, socketio
from app.document import scheduler
from app.document.service.operations import delete_document_tree
from app.document.service.tts_reader import get_manager, TTSOptions
import os
from pathlib import Path
from werkzeug.utils import secure_filename
from datetime import datetime

document_bp = Blueprint('document_bp', __name__, template_folder="templates")

# --- Helper to ensure upload directory exists ---


def _joinedload_attr(model: Any, attr: str) -> Any:
    """Return a joinedload loader option while hiding SQLAlchemy typing from mypy."""
    return cast(Any, joinedload(getattr(model, attr)))


def _active_workspace_ids(user: User) -> list[int]:
    """Return the list of active workspace IDs for the given user."""
    memberships = user.workspace_memberships.filter(WorkspaceMember.status == 'active').all()
    return [int(m.workspace_id) for m in memberships if getattr(m, "workspace_id", None) is not None]


def ensure_upload_dir():
    # Use instance_path for user-uploaded content
    upload_folder = os.path.join(current_app.instance_path, 'uploads', 'documents')
    os.makedirs(upload_folder, exist_ok=True)
    return upload_folder

# --- List Documents Route ---
@document_bp.route('/list', methods=['GET'])
@login_required  # Protect this route
def list_documents() -> ResponseReturnValue:
    """
    Lists documents from workspaces the current user is a member of.
    Also provides the list of workspaces for the upload form dropdown.
    """
    user = cast(User, current_user)
    workspace_ids = _active_workspace_ids(user)

    documents_query = (
        Document.query.options(
            _joinedload_attr(Document, "workspace"),
            _joinedload_attr(Document, "uploader"),
        )
        .filter(Document.workspace_id.in_(workspace_ids))
        .order_by(Document.uploaded_at.desc())
    )
    documents: list[Document] = list(documents_query.all())

    user_workspaces_query = (
        Workspace.query.filter(Workspace.workspace_id.in_(workspace_ids))
        .order_by(Workspace.name)
    )
    user_workspaces: list[Workspace] = list(user_workspaces_query.all())

    return render_template('document_list.html', documents=documents, workspaces=user_workspaces)

# --- Document Upload Form route ---
@document_bp.route('/upload', methods=['GET'])
@login_required  # Protect this route
def show_upload_form() -> ResponseReturnValue:
    """
    show the upload form
    """
    user = cast(User, current_user)
    workspace_ids = _active_workspace_ids(user)

    documents_query = (
        Document.query.options(
            _joinedload_attr(Document, "workspace"),
            _joinedload_attr(Document, "uploader"),
        )
        .filter(Document.workspace_id.in_(workspace_ids))
        .order_by(Document.uploaded_at.desc())
    )
    documents: list[Document] = list(documents_query.all())

    user_workspaces_query = (
        Workspace.query.filter(Workspace.workspace_id.in_(workspace_ids))
        .order_by(Workspace.name)
    )
    user_workspaces: list[Workspace] = list(user_workspaces_query.all())

    return render_template('document_upload.html', documents=documents, workspaces=user_workspaces)

# --- Document Upload Route ---
@document_bp.route('/upload', methods=['POST'])
@login_required  # Protect this route
def upload_document() -> ResponseReturnValue:
    """Handles the document upload form submission."""
    workspace_id = request.form.get('workspace_id', type=int)  # Get workspace ID early for redirects

    # --- Validation ---
    if not workspace_id:
         flash('Please select a workspace.', 'danger')
         # Redirect back to the general upload form or list if no workspace ID provided
         return redirect(url_for('document_bp.show_upload_form')) # Or list_documents

    # Verify user is a member of the selected workspace
    is_member = current_user.workspace_memberships.filter_by(
        workspace_id=workspace_id,
        status='active'
    ).first()
    if not is_member:
        flash('You do not have permission to upload to this workspace.', 'danger')
        # Redirect to a safe page like dashboard or workspace list
        return redirect(url_for('main.dashboard')) # Or wherever appropriate

    if 'file' not in request.files:
        flash('No file part selected.', 'danger')
        # Redirect back to the workspace details page where the upload was likely initiated
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))

    file = request.files['file']
    if file.filename == '':
        flash('No file selected.', 'danger')
        # Redirect back to the workspace details page
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))

    # Get other form data
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip() # Get description

    if file:
        filename_raw = file.filename or ""
        original_filename = secure_filename(filename_raw)
        if not title:
            title = original_filename # Use filename if title is empty

        upload_folder = ensure_upload_dir()
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        unique_filename = f"{current_user.user_id}_{workspace_id}_{timestamp}_{original_filename}"
        file_path_abs = os.path.join(upload_folder, unique_filename)
        file_path_rel = os.path.join('uploads', 'documents', unique_filename)

        try:
            file.save(file_path_abs)
            file_size = os.path.getsize(file_path_abs)

            assert workspace_id is not None

            new_document = Document()
            new_document.title = title
            new_document.description = description or None
            new_document.file_name = original_filename
            new_document.file_path = file_path_rel
            new_document.uploaded_by_id = int(current_user.user_id)
            new_document.file_size = file_size
            new_document.workspace_id = workspace_id
            db.session.add(new_document)
            db.session.commit()
            flash(f'Document "{title}" uploaded successfully!', 'success') # Simplified message

            # *** <<< CHANGE HERE: Redirect to workspace details on success >>> ***
            return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error uploading document: {e}")
            flash('An error occurred during upload. Please try again.', 'danger')
            if os.path.exists(file_path_abs):
                try:
                    os.remove(file_path_abs)
                except OSError as rm_err:
                     current_app.logger.error(f"Error removing failed upload file {file_path_abs}: {rm_err}")

            # *** <<< CHANGE HERE: Redirect to workspace details on error >>> ***
            # Redirect back to the workspace details page even on error,
            # as that's likely where the user initiated the upload.
            return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))

    # Fallback redirect if 'file' object somehow doesn't evaluate to True
    # *** <<< CHANGE HERE: Fallback redirect to workspace details >>> ***
    flash('File upload failed unexpectedly.', 'warning')
    return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))

# --- Document Preview Route ---
@document_bp.route('/preview/<int:document_id>', methods=['GET'])
@login_required  # Protect this route
def preview_document(document_id: int) -> ResponseReturnValue:
    """Displays details of a single document."""
    # Fetch document and eagerly load related workspace and uploader
    document = (
        Document.query.options(
            _joinedload_attr(Document, "workspace"),
            _joinedload_attr(Document, "uploader"),
            _joinedload_attr(Document, "chunks"),
            _joinedload_attr(Document, "processing_logs"),
        )
        .get_or_404(document_id)
    )

    # --- Permission Check ---
    # Verify user is a member of the workspace this document belongs to
    if document.workspace_id not in _active_workspace_ids(cast(User, current_user)):
        flash("You don't have permission to view this document.", "danger")
        return redirect(url_for('document_bp.list_documents'))

    # Construct the absolute path to check if the file exists (optional but good practice)
    # Note: This doesn't serve the file, just checks existence. Serving files requires a dedicated route.
    full_file_path = os.path.join(current_app.instance_path, document.file_path)
    file_exists = os.path.exists(full_file_path)
    if not file_exists:
         flash("The document file seems to be missing.", "warning")
         # Decide if you still want to show the preview page or redirect

    document.increment_access_count()
    db.session.commit()

    logs = sorted(document.processing_logs, key=lambda l: l.created_at or l.started_at or datetime.min, reverse=True)

    latest_audio = None
    audio_url = None
    if getattr(document, "audios", None):
        latest_audio = max(
            (a for a in document.audios if a),
            key=lambda a: a.created_at or datetime.min,
            default=None,
        )
        if latest_audio:
            audio_url = url_for('document_bp.document_audio', document_id=document.id, audio_id=latest_audio.id)

    return render_template(
        'document_details.html',
        document=document,
        file_exists=file_exists,
        processing_logs=logs,
        chunks=document.chunks,
        latest_audio=latest_audio,
        audio_url=audio_url,
    )

# --- Document Delete Route ---
@document_bp.route('/delete/<int:document_id>', methods=['POST']) # Use POST for destructive actions
@login_required # Protect this route
def delete_document(document_id):
    """Deletes a document record and its associated file."""
    document = Document.query.get_or_404(document_id)
    # Store workspace_id early for redirection
    workspace_id_for_redirect = document.workspace_id

    # --- Permission Check ---
    # User must either be the uploader OR an admin/manager of the workspace
    is_owner = document.uploaded_by_id == current_user.user_id
    member_info = current_user.workspace_memberships.filter_by(
        workspace_id=document.workspace_id,
        status='active'
    ).first()
    is_workspace_admin = member_info and member_info.role in ['admin', 'manager'] # Adjust roles as needed

    if not (is_owner or is_workspace_admin):
        flash("You don't have permission to delete this document.", "danger")
        # Redirect back to the workspace details page even on permission error
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id_for_redirect))

    result = delete_document_tree(document, actor_id=current_user.user_id)
    flash(result.message, result.category)

    if not result.success:
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id_for_redirect))

    # *** <<< CHANGE HERE: Redirect to workspace details >>> ***
    # Redirect back to the workspace details page regardless of success/error during deletion process
    return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id_for_redirect))


# --- Serve Document File (inline or download) ---
@document_bp.route('/file/<int:document_id>')
@login_required
def serve_document_file(document_id: int):
    """Serve the physical document file from the instance uploads folder.

    Security: Only members of the owning workspace can access the file.
    By default files are served inline; append ?download=1 to force download.
    """
    doc = Document.query.get_or_404(document_id)

    # Permission: allow if user is an active member of the workspace OR
    # an accepted participant in any workshop that links this document.
    is_member = current_user.workspace_memberships.filter_by(
        workspace_id=doc.workspace_id,
        status='active'
    ).first()
    if not is_member:
        # Check linked workshops where user participates
        link_q = WorkshopDocument.query.filter_by(document_id=doc.id)
        linked_ws_ids = [l.workshop_id for l in link_q.all()]
        allowed = False
        if linked_ws_ids:
            part = WorkshopParticipant.query.filter(
                WorkshopParticipant.workshop_id.in_(linked_ws_ids),
                WorkshopParticipant.user_id == current_user.user_id,
                WorkshopParticipant.status.in_(['accepted','invited','participant','active'])
            ).first()
            if part:
                allowed = True
        if not allowed:
            flash("You don't have permission to access this document.", "danger")
            abort(403)

    # Build absolute path under instance folder
    abs_path = os.path.join(current_app.instance_path, doc.file_path)
    if not os.path.exists(abs_path):
        current_app.logger.warning(f"Requested document file missing: {abs_path}")
        abort(404)

    directory = os.path.dirname(abs_path)
    filename = os.path.basename(abs_path)
    # Serve inline by default; allow forcing download via query param
    as_attachment = request.args.get('download') in {'1', 'true', 'True'}
    # Let Flask infer mimetype from filename extension
    return send_from_directory(directory, filename, as_attachment=as_attachment)


# ---------------- Document Processing APIs ----------------


def _user_can_process(document: Document) -> bool:
    if document.uploaded_by_id == current_user.user_id:
        return True
    membership = current_user.workspace_memberships.filter_by(
        workspace_id=document.workspace_id,
        status='active',
    ).first()
    if membership and membership.role in {'admin', 'manager'}:
        return True
    return False


@document_bp.route('/<int:document_id>/process', methods=['POST'])
@login_required
def process_document(document_id: int) -> ResponseReturnValue:
    document = (
        Document.query.options(_joinedload_attr(Document, "workspace"))
        .get_or_404(document_id)
    )

    if not _user_can_process(document):
        return jsonify({'error': "You don't have permission to process this document."}), 403

    force = False
    if request.is_json:
        payload = cast(Mapping[str, Any], request.get_json(silent=True) or {})
        force = bool(payload.get('force'))
    else:
        force = request.form.get('force') in {'1', 'true', 'True'} or request.args.get('force') in {'1', 'true', 'True'}

    active_job = (
        DocumentProcessingJob.query.filter_by(document_id=document.id)
        .filter(DocumentProcessingJob.status.in_(['pending', 'in_progress']))
        .order_by(DocumentProcessingJob.created_at.desc())
        .first()
    )

    if active_job and not force:
        return (
            jsonify(
                {
                    'status': active_job.status,
                    'message': 'Document already processing',
                    'jobId': active_job.id,
                }
            ),
            409,
        )

    if document.processing_status in {'queued', 'processing'} and active_job is None:
        current_app.logger.warning(
            'Document %s stuck in %s without active job; resetting before enqueue',
            document.id,
            document.processing_status,
        )

    document.processing_status = 'queued'
    document.processing_started_at = None
    db.session.commit()

    job = scheduler.enqueue(document_id, force=force)

    payload = {
        'documentId': document_id,
        'stage': 'queued',
        'status': 'queued',
    }
    room = f"workspace_{document.workspace_id}" if document.workspace_id else None
    if room:
        socketio.emit('doc_processing_progress', payload, to=room)
    else:
        socketio.emit('doc_processing_progress', payload)

    return jsonify({'status': 'queued', 'jobId': job.id, 'force': force}), 202


@document_bp.route('/<int:document_id>/chunks.json', methods=['GET'])
@login_required
def document_chunks(document_id: int) -> ResponseReturnValue:
    document = (
        Document.query.options(_joinedload_attr(Document, "chunks"))
        .get_or_404(document_id)
    )
    if document.workspace_id not in _active_workspace_ids(cast(User, current_user)):
        return jsonify({'error': "You don't have permission to view this document."}), 403

    chunks = [
        {
            'id': chunk.id,
            'order': chunk.meta_data.get('order') if chunk.meta_data else index,
            'content': chunk.content,
            'metadata': chunk.meta_data or {},
        }
        for index, chunk in enumerate(sorted(document.chunks, key=lambda c: c.meta_data.get('order', c.id) if c.meta_data else c.id))
    ]

    return jsonify({'documentId': document_id, 'chunks': chunks, 'count': len(chunks)})


@document_bp.route('/<int:document_id>/processing/logs', methods=['GET'])
@login_required
def document_processing_logs(document_id: int) -> ResponseReturnValue:
    document = Document.query.get_or_404(document_id)
    if document.workspace_id not in _active_workspace_ids(cast(User, current_user)):
        return jsonify({'error': "You don't have permission to view this document."}), 403

    logs = (
        DocumentProcessingLog.query.filter_by(document_id=document_id)
        .order_by(DocumentProcessingLog.created_at.desc(), DocumentProcessingLog.started_at.desc())
        .all()
    )

    serialized = [
        {
            'id': log.id,
            'stage': log.stage,
            'status': log.status,
            'startedAt': log.started_at.isoformat() if log.started_at else None,
            'completedAt': log.completed_at.isoformat() if log.completed_at else None,
            'error': log.error_message,
            'processedPages': log.processed_pages,
            'totalPages': log.total_pages,
        }
        for log in logs
    ]

    return jsonify({'documentId': document_id, 'logs': serialized})


@document_bp.route('/<int:document_id>/detail.json', methods=['GET'])
@login_required
def document_detail(document_id: int):
    document = Document.query.get_or_404(document_id)
    memberships = current_user.workspace_memberships.filter_by(status='active').all()
    allowed_workspace_ids = {m.workspace_id for m in memberships}
    if document.workspace_id not in allowed_workspace_ids and document.uploaded_by_id != current_user.user_id:
        return jsonify({'error': "You don't have permission to view this document."}), 403

    latest_audio = None
    audio_payload = None
    if getattr(document, 'audios', None):
        latest_audio = max(
            (a for a in document.audios if a),
            key=lambda a: a.created_at or datetime.min,
            default=None,
        )
        if latest_audio:
            audio_payload = {
                'id': latest_audio.id,
                'url': url_for('document_bp.document_audio', document_id=document.id, audio_id=latest_audio.id),
                'durationSeconds': latest_audio.duration_seconds,
            }

    chunks = getattr(document, 'chunks', []) or []

    payload = {
        'documentId': document.id,
        'title': document.title,
        'status': document.processing_status,
        'summary': document.summary,
        'description': document.description,
        'markdown': document.markdown,
        'ttsScript': document.tts_script,
        'contentSha': document.content_sha256,
        'lastProcessedAt': document.last_processed_at.isoformat() if document.last_processed_at else None,
        'audio': audio_payload,
        'chunkCount': len(chunks),
    }

    return jsonify(payload)


@document_bp.route('/<int:document_id>/audio', methods=['GET'])
@login_required
def document_audio(document_id: int):
    document = Document.query.get_or_404(document_id)

    is_member = current_user.workspace_memberships.filter_by(
        workspace_id=document.workspace_id,
        status='active'
    ).first()
    if not is_member and document.uploaded_by_id != current_user.user_id:
        link_q = WorkshopDocument.query.filter_by(document_id=document.id)
        linked_ws_ids = [l.workshop_id for l in link_q.all()]
        allowed = False
        if linked_ws_ids:
            part = WorkshopParticipant.query.filter(
                WorkshopParticipant.workshop_id.in_(linked_ws_ids),
                WorkshopParticipant.user_id == current_user.user_id,
                WorkshopParticipant.status.in_(['accepted','invited','participant','active'])
            ).first()
            if part:
                allowed = True
        if not allowed:
            return jsonify({'error': "You don't have permission to access this audio."}), 403

    audio_id = request.args.get('audio_id', type=int)
    query = DocumentAudio.query.filter_by(document_id=document_id)
    audio = None
    if audio_id:
        audio = query.filter_by(id=audio_id).first()
    else:
        audio = query.order_by(DocumentAudio.created_at.desc()).first()

    if not audio:
        abort(404)

    abs_path = os.path.join(current_app.instance_path, audio.audio_file_path)
    if not os.path.exists(abs_path):
        current_app.logger.warning("Requested document audio missing: %s", abs_path)
        abort(404)

    directory = os.path.dirname(abs_path)
    filename = os.path.basename(abs_path)
    return send_from_directory(directory, filename, as_attachment=False)


@document_bp.route('/<int:document_id>/audio/generate', methods=['POST'])
@login_required
def generate_document_audio(document_id: int):
    document = Document.query.get_or_404(document_id)

    if not _user_can_process(document):
        return jsonify({'error': "You don't have permission to generate narration for this document."}), 403

    payload = request.get_json(silent=True) or {}
    force = bool(payload.get('force')) if isinstance(payload, dict) else False
    provider = payload.get('provider') if isinstance(payload, dict) else None
    voice = payload.get('voice') if isinstance(payload, dict) else None
    fmt = payload.get('format') if isinstance(payload, dict) else None
    speed = payload.get('speed') if isinstance(payload, dict) else None

    if not document.tts_script:
        return jsonify({'error': 'Narration script not available yet. Run AI processing first.'}), 400

    try:
        speed_value = float(speed) if speed is not None else 1.0
    except (TypeError, ValueError):
        speed_value = 1.0
    speed_value = min(max(speed_value, 0.5), 2.0)
    fmt_value = fmt.lower() if isinstance(fmt, str) else 'wav'
    if fmt_value not in {'wav', 'mp3'}:
        fmt_value = 'wav'

    manager = get_manager()
    options = TTSOptions(provider=provider or None, voice=voice or None, speed=speed_value, fmt=fmt_value)

    try:
        audio = manager.ensure_audio(document, options=options, force=force)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:  # pragma: no cover - provider failure path
        db.session.rollback()
        current_app.logger.exception('Failed to generate narration audio for document %s: %s', document_id, exc)
        return jsonify({'error': 'Failed to generate narration audio.'}), 500

    audio_payload = {
        'audioId': audio.id,
        'url': url_for('document_bp.document_audio', document_id=document.id, audio_id=audio.id),
        'durationSeconds': audio.duration_seconds,
    }

    return jsonify({'documentId': document.id, 'audio': audio_payload}), 200



