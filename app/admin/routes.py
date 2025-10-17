"""Administrative back-office routes."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from io import BytesIO
from typing import Any, Dict, List, Optional, cast

from flask import (
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from flask_mail import Message
from flask_wtf import FlaskForm

from app.assistant.memory.service import AgentMemoryService
from app.assistant.memory.settings import AgentCoreMemorySettings
from app.extensions import db, mail
from app.models import (
    Document,
    DocumentAudio,
    DocumentProcessingLogArchive,
    User,
    Workshop,
    WorkshopDocument,
)
from app.models_admin import AdminLog, UserSession
from app.models_assistant import ChatThread, ChatTurn
from sqlalchemy.orm import joinedload, selectinload

from . import admin_bp
from .config_manager import ConfigManager
from .dashboard import AdminDashboard
from .decorators import admin_required
from .forms import DocumentUploadForm, SystemConfigForm, UserCreationForm, UserManagementForm
from .health_monitor import HealthMonitor
from .document_admin import DocumentAdmin
from .user_management import UserManager
from .workshop_admin import WorkshopAdmin
from app.document.service.operations import delete_document_tree


MAX_MEMORY_PAYLOAD_BYTES = 16_384


NAV_ITEMS = (
    {"endpoint": "admin.dashboard", "label": "Dashboard"},
    {"endpoint": "admin.user_list", "label": "Users"},
    {"endpoint": "admin.workshop_overview", "label": "Workshops"},
    {"endpoint": "admin.documents", "label": "Documents"},
    {"endpoint": "admin.sessions", "label": "Sessions"},
    {"endpoint": "admin.memory_overview", "label": "Memory"},
    {"endpoint": "admin.bedrock_settings", "label": "Bedrock"},
    {"endpoint": "admin.system_settings", "label": "System"},
    {"endpoint": "admin.logs", "label": "Audit Logs"},
)


@admin_bp.app_context_processor
def inject_admin_defaults() -> Dict[str, object]:
    return {
        "admin_nav_items": NAV_ITEMS,
        "current_year": datetime.utcnow().year,
    }


class CSRFOnlyForm(FlaskForm):
    """Utility form that provides CSRF protection for POST actions."""


def _load_memory_entries() -> List[Dict[str, str]]:
    overrides = ConfigManager.load_overrides()
    entries = overrides.get("assistant_memory", {})
    if not isinstance(entries, dict):
        return []
    normalized: List[Dict[str, str]] = []
    for memory_id, payload in sorted(entries.items(), key=lambda item: item[0]):
        pretty = str(payload)
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = None
            else:
                pretty = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=True)
        normalized.append({
            "id": str(memory_id),
            "data": pretty,
        })
    return normalized


def _populate_system_form(form: SystemConfigForm, config: Dict[str, Dict[str, Optional[str]]]) -> None:
    app_config = config.get("app", {})
    form.app_name.data = app_config.get("name") or ""
    form.default_timezone.data = app_config.get("timezone") or ""
    form.workshop_default_duration.data = app_config.get("workshop_default_duration") or ""
    form.max_workshop_participants.data = app_config.get("max_workshop_participants") or ""
    form.enable_ai_features.data = bool(app_config.get("enable_ai_features"))
    form.bedrock_model_id.data = app_config.get("bedrock_model_id") or ""

    mail_config = config.get("mail", {})
    form.smtp_server.data = mail_config.get("server") or ""
    form.smtp_port.data = mail_config.get("port") or ""
    form.mail_username.data = mail_config.get("username") or ""


def _send_welcome_email(email: str, display_name: str) -> bool:
    """Send a welcome email to a newly created user."""

    login_url = url_for("auth_bp.login", _external=True)
    subject = f"Welcome to {current_app.config.get('APP_NAME', 'BrainStorm X')}"
    sender = current_app.config.get("MAIL_DEFAULT_SENDER") or current_app.config.get("MAIL_USERNAME")
    if sender is None:
        sender = f"no-reply@{request.host}" if request else "no-reply@brainstormx.local"

    msg = Message(subject=subject, recipients=[email], sender=sender)
    greeting_name = display_name or "there"
    msg.body = (
        f"Hi {greeting_name},\n\n"
        "An administrator created an account for you on BrainStorm X.\n"
        f"You can sign in using the email associated with this message at {login_url}.\n\n"
        "If you were not expecting this invitation please ignore this email.\n\n"
        "Thanks,\nBrainStorm X"
    )
    msg.html = (
        f"<p>Hi {greeting_name},</p>"
        "<p>An administrator created an account for you on BrainStorm X.</p>"
        f"<p><a href=\"{login_url}\">Click here to sign in</a> with your email address.</p>"
        "<p>If you were not expecting this invitation please ignore this email.</p>"
        "<p>Thanks,<br>BrainStorm X Team</p>"
    )

    try:
        mail.send(msg)
        return True
    except Exception as exc:  # pragma: no cover - mail delivery is environment specific
        current_app.logger.warning("welcome_email_failed", extra={"error": str(exc)})
        return False


@admin_bp.route("/")
@admin_bp.route("/dashboard")
@login_required
@admin_required
def dashboard():
    metrics = AdminDashboard.get_system_metrics()
    recent_logs = AdminDashboard.recent_admin_logs(limit=8)
    health = HealthMonitor.get_system_health()
    return render_template(
        "dashboard.html",
        metrics=metrics,
        recent_logs=recent_logs,
        health=health,
    metrics_api=url_for("admin_api.get_metrics"),
    )


@admin_bp.route("/users")
@login_required
@admin_required
def user_list():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)
    search_raw = request.args.get("search", "")
    search = search_raw.strip() or None

    pagination = UserManager.paginate_users(page=page, per_page=per_page, search=search)
    return render_template(
        "users.html",
        pagination=pagination,
        users=pagination.items,
        search=search_raw,
        delete_form=CSRFOnlyForm(),
    )


@admin_bp.route("/users/create", methods=["GET", "POST"])
@login_required
@admin_required
def create_user():
    form = UserCreationForm()
    if form.validate_on_submit():
        try:
            user = UserManager.create_user(
                {
                    "email": form.email.data,
                    "password": form.password.data,
                    "role": form.role.data,
                    "first_name": form.first_name.data,
                    "last_name": form.last_name.data,
                    "username": form.username.data,
                    "job_title": form.job_title.data,
                    "organization": form.organization.data,
                    "email_verified": form.email_verified.data,
                    "is_public_profile": form.is_public_profile.data,
                }
            )
        except ValueError as exc:
            flash(str(exc), "danger")
        else:
            if form.send_welcome_email.data:
                sent = _send_welcome_email(user.email, user.display_name)
                if not sent:
                    flash("User created, but the welcome email could not be sent.", "warning")
                else:
                    flash("User created and welcome email sent.", "success")
            else:
                flash("User created successfully.", "success")
            return redirect(url_for("admin.user_detail", user_id=user.user_id))

    return render_template("user_form.html", form=form, mode="create")


@admin_bp.route("/users/<int:user_id>")
@login_required
@admin_required
def user_detail(user_id: int):
    context = UserManager.get_user_with_metrics(user_id)
    return render_template(
        "user_detail.html",
        user=context["user"],
        metrics=context["metrics"],
        delete_form=CSRFOnlyForm(),
    )


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_user(user_id: int):
    user = User.query.get_or_404(user_id)
    form = UserManagementForm(obj=user)
    form.email.render_kw = {"readonly": True}

    if not form.is_submitted():
        form.email.data = user.email
        form.first_name.data = user.first_name or ""
        form.last_name.data = user.last_name or ""
        form.username.data = user.username or ""
        form.job_title.data = user.job_title or ""
        form.organization.data = user.organization or ""
        form.role.data = user.role
        form.email_verified.data = user.email_verified
        form.is_public_profile.data = user.is_public_profile

    if form.validate_on_submit():
        try:
            UserManager.update_user(
                user,
                {
                    "first_name": form.first_name.data,
                    "last_name": form.last_name.data,
                    "username": form.username.data,
                    "job_title": form.job_title.data,
                    "organization": form.organization.data,
                    "role": form.role.data,
                    "email_verified": form.email_verified.data,
                    "is_public_profile": form.is_public_profile.data,
                },
            )
        except ValueError as exc:
            flash(str(exc), "danger")
        else:
            flash("User updated successfully.", "success")
            return redirect(url_for("admin.user_detail", user_id=user.user_id))

    return render_template("user_form.html", form=form, user=user, mode="edit")


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id: int):
    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        abort(400)

    if user_id == current_user.user_id:
        flash("You cannot delete your own administrator account.", "warning")
        return redirect(url_for("admin.user_detail", user_id=user_id))

    user = User.query.get_or_404(user_id)
    UserManager.delete_user(user)
    flash("User deleted.", "success")
    return redirect(url_for("admin.user_list"))


@admin_bp.route("/documents")
@login_required
@admin_required
def documents():
    workspace_filter = request.args.get("workspace_id", type=int)
    status_filter = request.args.get("status", default="all")
    search_query = (request.args.get("search") or "").strip()

    upload_form = DocumentUploadForm()
    workspace_choices = list(DocumentAdmin.workspace_choices())
    upload_form.workspace_id.choices = cast(Any, workspace_choices)

    query = (
        Document.query.options(
            selectinload(cast(Any, getattr(Document, "workspace"))),
            selectinload(cast(Any, getattr(Document, "uploader"))),
            selectinload(cast(Any, getattr(Document, "child_documents"))),
        )
        .order_by(Document.uploaded_at.desc())
    )

    if workspace_filter:
        query = query.filter(Document.workspace_id == workspace_filter)

    status_filter = status_filter if status_filter in {"all", "active", "archived", "processing", "failed"} else "all"
    if status_filter == "active":
        query = query.filter(getattr(Document, "is_archived").is_(False))
    elif status_filter == "archived":
        query = query.filter(getattr(Document, "is_archived").is_(True))
    elif status_filter == "processing":
        query = query.filter(getattr(Document, "processing_status").in_(["processing", "queued"]))
    elif status_filter == "failed":
        query = query.filter(getattr(Document, "processing_status") == "failed")

    if search_query:
        query = query.filter(Document.title.ilike(f"%{search_query}%"))

    documents_result = list(query.all())
    stats = DocumentAdmin.summarize_documents(documents_result)

    delete_form = CSRFOnlyForm()
    status_options = [
        ("all", "All documents"),
        ("active", "Active"),
        ("processing", "Processing"),
        ("failed", "Failed"),
        ("archived", "Archived"),
    ]

    workspace_lookup = {ws_id: label for ws_id, label in workspace_choices}

    return render_template(
        "documents.html",
        documents=documents_result,
        upload_form=upload_form,
        delete_form=delete_form,
        stats=stats,
        status_options=status_options,
        status_filter=status_filter,
        search_query=search_query,
        workspace_filter=workspace_filter,
        workspace_lookup=workspace_lookup,
        workspace_choices=workspace_choices,
    )


@admin_bp.route("/documents/upload", methods=["POST"])
@login_required
@admin_required
def upload_document_admin():
    upload_form = DocumentUploadForm()
    workspace_choices = list(DocumentAdmin.workspace_choices())
    upload_form.workspace_id.choices = cast(Any, workspace_choices)

    if not upload_form.validate_on_submit():
        error_messages = [msg for msgs in upload_form.errors.values() for msg in msgs]
        message = error_messages[0] if error_messages else "Upload failed due to validation errors."
        flash(message, "danger")
        return redirect(url_for("admin.documents"))

    try:
        new_document = DocumentAdmin.save_upload(
            form_data={
                "workspace_id": upload_form.workspace_id.data,
                "title": upload_form.title.data,
                "description": upload_form.description.data,
            },
            file_storage=upload_form.file.data,
            actor_id=int(current_user.user_id),
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin.documents"))
    except RuntimeError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin.documents"))

    AdminLog.log_action(
        actor_id=current_user.user_id,
        action="document_uploaded",
        entity_type="Document",
        entity_id=str(new_document.id),
        metadata={
            "workspace_id": new_document.workspace_id,
            "file_name": new_document.file_name,
            "file_size": new_document.file_size,
        },
    )
    flash(f'Document "{new_document.title}" uploaded successfully.', "success")
    return redirect(url_for("admin.document_detail", document_id=new_document.id))


@admin_bp.route("/documents/<int:document_id>")
@login_required
@admin_required
def document_detail(document_id: int):
    document = (
        Document.query.options(
            selectinload(cast(Any, getattr(Document, "workspace"))),
            selectinload(cast(Any, getattr(Document, "uploader"))),
            selectinload(cast(Any, getattr(Document, "child_documents"))),
            selectinload(cast(Any, getattr(Document, "parent_document"))),
            selectinload(cast(Any, getattr(Document, "audios"))),
            selectinload(cast(Any, getattr(Document, "processing_logs"))),
        )
        .filter(Document.id == document_id)
        .first()
    )
    if document is None:
        abort(404)

    workshop_link_records = (
        WorkshopDocument.query.options(
            selectinload(cast(Any, getattr(WorkshopDocument, "workshop")))
        )
        .filter_by(document_id=document_id)
        .all()
    )

    logs = sorted(
        list(document.processing_logs or []),
        key=lambda entry: entry.created_at or entry.started_at or datetime.min,
        reverse=True,
    )
    archived_logs = (
        DocumentProcessingLogArchive.query.filter_by(document_id=document_id)
        .order_by(DocumentProcessingLogArchive.archived_at.desc())
        .limit(50)
        .all()
    )

    latest_audio = None
    if getattr(document, "audios", None):
        latest_audio = max(
            document.audios,
            key=lambda audio: audio.created_at or datetime.min,
            default=None,
        )

    file_path = Path(current_app.instance_path) / (document.file_path or "")
    file_exists = file_path.exists()

    version_children = sorted(
        list(document.child_documents or []),
        key=lambda doc: (doc.version or 0, doc.uploaded_at or datetime.min),
    )

    workshop_links = [
        link.workshop for link in workshop_link_records if link.workshop is not None
    ]

    delete_form = CSRFOnlyForm()

    return render_template(
        "document_detail.html",
        document=document,
        file_exists=file_exists,
        latest_audio=latest_audio,
        processing_logs=logs,
        archived_logs=archived_logs,
        version_children=version_children,
        delete_form=delete_form,
        workshop_links=workshop_links,
    )


@admin_bp.route("/documents/<int:document_id>/download")
@login_required
@admin_required
def document_download(document_id: int):
    document = Document.query.get_or_404(document_id)
    absolute_path = Path(current_app.instance_path) / (document.file_path or "")
    if not absolute_path.exists():
        flash("The document file could not be found on disk.", "warning")
        return redirect(url_for("admin.document_detail", document_id=document_id))

    return send_file(absolute_path, as_attachment=True, download_name=document.file_name)


@admin_bp.route("/documents/<int:document_id>/audio/<int:audio_id>")
@login_required
@admin_required
def document_audio_admin(document_id: int, audio_id: int):
    audio = (
        DocumentAudio.query.filter_by(document_id=document_id, id=audio_id)
        .order_by(DocumentAudio.created_at.desc())
        .first()
    )
    if audio is None:
        abort(404)

    absolute_path = Path(current_app.instance_path) / (audio.audio_file_path or "")
    if not absolute_path.exists():
        flash("The audio file could not be found on disk.", "warning")
        return redirect(url_for("admin.document_detail", document_id=document_id))

    return send_file(absolute_path, as_attachment=False, download_name=absolute_path.name)


@admin_bp.route("/documents/<int:document_id>/delete", methods=["POST"])
@login_required
@admin_required
def document_delete(document_id: int):
    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        abort(400)

    document = Document.query.get_or_404(document_id)
    workspace_id = document.workspace_id
    result = delete_document_tree(document, actor_id=current_user.user_id)

    flash(result.message, result.category or "info")

    if result.success:
        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="document_deleted",
            entity_type="Document",
            entity_id=str(document_id),
            metadata={"workspace_id": workspace_id},
        )
        return redirect(url_for("admin.documents", workspace_id=workspace_id))

    return redirect(url_for("admin.document_detail", document_id=document_id))


@admin_bp.route("/sessions")
@login_required
@admin_required
def sessions():
    sessions = UserSession.query.order_by(UserSession.last_activity.desc()).all()
    return render_template(
        "sessions.html",
        sessions=sessions,
        revoke_form=CSRFOnlyForm(),
    )


@admin_bp.route("/sessions/<int:session_id>/revoke", methods=["POST"])
@login_required
@admin_required
def revoke_session(session_id: int):
    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        abort(400)

    session = UserSession.query.get_or_404(session_id)
    if session.is_active:
        session.is_active = False
        db.session.commit()
        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="session_revoked",
            entity_type="UserSession",
            entity_id=str(session_id),
            metadata={"user_id": session.user_id},
        )
        flash("Session revoked.", "success")
    else:
        flash("Session already inactive.", "info")

    return redirect(url_for("admin.sessions"))


@admin_bp.route("/memory", methods=["GET", "POST"])
@login_required
@admin_required
def memory_overview():
    form = CSRFOnlyForm()
    settings = AgentCoreMemorySettings.from_app()
    memory_service = AgentMemoryService(settings) if settings.enabled else None

    search_params = {
        "workshop_id": request.args.get("workshop_id", type=int),
        "user_id": request.args.get("user_id", type=int),
        "thread_id": request.args.get("thread_id", type=int),
        "query": (request.args.get("query") or "").strip(),
        "top_k": request.args.get("top_k", type=int),
    }
    memory_search_results = None
    memory_search_error = None
    memory_search_meta: Dict[str, object] | None = None

    if request.method == "POST":
        if not form.validate_on_submit():
            flash("Invalid request.", "danger")
            return redirect(url_for("admin.memory_overview"))
        memory_id = (request.form.get("memory_id") or "").strip()
        memory_data = (request.form.get("memory_data") or "").strip()

        if not memory_id:
            flash("Memory ID is required.", "danger")
            return redirect(url_for("admin.memory_overview"))

        if len(memory_data.encode("utf-8")) > MAX_MEMORY_PAYLOAD_BYTES:
            flash("Memory data is too large (16 KB limit).", "danger")
            return redirect(url_for("admin.memory_overview"))

        try:
            parsed_payload = json.loads(memory_data)
        except json.JSONDecodeError as exc:  # pragma: no cover - guard rail
            flash(f"Memory data must be valid JSON ({exc.msg}).", "danger")
            return redirect(url_for("admin.memory_overview"))

        if not isinstance(parsed_payload, dict):
            flash("Memory data must be a JSON object.", "danger")
            return redirect(url_for("admin.memory_overview"))

        normalized_payload = json.dumps(parsed_payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)

        ConfigManager.update_config("assistant_memory", memory_id, normalized_payload)
        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="memory_updated",
            entity_type="AssistantMemory",
            entity_id=memory_id,
            metadata={"length": len(normalized_payload)},
        )
        flash("Memory entry updated.", "success")
        return redirect(url_for("admin.memory_overview"))

    if request.method == "GET" and search_params["query"]:
        if not settings.enabled:
            memory_search_error = "AgentCore Memory is disabled."
        elif search_params["workshop_id"] is None:
            memory_search_error = "Workshop ID is required to query AgentCore Memory."
        elif memory_service is None:
            memory_search_error = "AgentCore Memory client is unavailable."
        else:
            original_top_k = memory_service.settings.top_k
            try:
                top_k_override = search_params["top_k"]
                if top_k_override is not None and top_k_override > 0:
                    memory_service.settings.top_k = top_k_override

                result = memory_service.retrieve(
                    query=search_params["query"],
                    workshop_id=search_params["workshop_id"],
                    user_id=search_params["user_id"],
                    thread_id=search_params["thread_id"],
                )
                memory_search_results = result.snippets
                memory_search_meta = {
                    "latency_ms": result.latency_ms,
                    "namespaces": result.namespaces,
                    "errors": result.errors,
                    "count": len(result.snippets),
                }
            except Exception as exc:  # pragma: no cover - network errors
                memory_search_error = f"Failed to query AgentCore Memory: {exc}"
            finally:
                memory_service.settings.top_k = original_top_k

    entries = _load_memory_entries()
    memory_status = {
        "enabled": settings.enabled,
        "memory_id": settings.memory_id,
        "region": settings.region,
        "top_k": settings.top_k,
        "namespaces": settings.namespace_templates,
        "store_in_background": settings.store_in_background,
    }

    return render_template(
        "memory.html",
        memory_entries=entries,
        csrf_form=form,
        memory_status=memory_status,
        memory_search_results=memory_search_results,
        memory_search_error=memory_search_error,
        memory_search_meta=memory_search_meta,
        search_params=search_params,
    )


@admin_bp.route("/memory/<string:memory_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_memory(memory_id: str):
    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.memory_overview"))
    ConfigManager.update_config("assistant_memory", memory_id, None)
    AdminLog.log_action(
        actor_id=current_user.user_id,
        action="memory_deleted",
        entity_type="AssistantMemory",
        entity_id=memory_id,
    )
    flash("Memory entry deleted.", "success")
    return redirect(url_for("admin.memory_overview"))


@admin_bp.route("/bedrock", methods=["GET", "POST"])
@login_required
@admin_required
def bedrock_settings():
    config = ConfigManager.get_environment_config()
    bedrock_config = config.get("bedrock", {}) if isinstance(config, dict) else {}
    form = CSRFOnlyForm()

    if request.method == "POST":
        if not form.validate_on_submit():
            flash("Invalid request.", "danger")
            return redirect(url_for("admin.bedrock_settings"))
        updates = {
            "model_id": (request.form.get("model_id") or "").strip() or None,
            "nova_pro": (request.form.get("nova_pro") or "").strip() or None,
            "image_model": (request.form.get("image_gen") or "").strip() or None,
            "video_model": (request.form.get("video_gen") or "").strip() or None,
            "speech_model": (request.form.get("speech_model") or "").strip() or None,
        }

        for key, value in updates.items():
            ConfigManager.update_config("bedrock", key, value)

        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="bedrock_config_updated",
            entity_type="Config",
            entity_id="bedrock",
            metadata={k: v for k, v in updates.items() if v is not None},
        )
        flash("Bedrock configuration updated.", "success")
        return redirect(url_for("admin.bedrock_settings"))

    context = {
        "bedrock_model_id": bedrock_config.get("model_id"),
        "bedrock_nova_pro": bedrock_config.get("nova_pro"),
        "bedrock_nova_image_gen": bedrock_config.get("image_model"),
        "bedrock_nova_video_gen": bedrock_config.get("video_model"),
        "bedrock_nova_speech": bedrock_config.get("speech_model"),
        "csrf_form": form,
    }
    return render_template("bedrock.html", **context)


@admin_bp.route("/workshops")
@login_required
@admin_required
def workshop_overview():
    analytics = WorkshopAdmin.get_workshop_analytics()
    workshops = Workshop.query.order_by(Workshop.updated_at.desc()).limit(25).all()
    return render_template(
        "workshop.html",
        analytics=analytics,
        workshops=workshops,
    )


@admin_bp.route("/workshops/<int:workshop_id>")
@login_required
@admin_required
def workshop_detail(workshop_id: int):
    workshop = Workshop.query.get_or_404(workshop_id)
    snapshot = WorkshopAdmin.workshop_snapshot(workshop)
    return render_template(
        "workshop_detail.html",
        workshop=workshop,
        snapshot=snapshot,
    )


@admin_bp.route("/workshops/<int:workshop_id>/export/<string:export_format>")
@login_required
@admin_required
def workshop_export(workshop_id: int, export_format: str):
    workshop = Workshop.query.get_or_404(workshop_id)
    filename_base = f"workshop-{workshop_id}"

    if export_format == "pdf":
        pdf_bytes = WorkshopAdmin.export_workshop_pdf(workshop)
        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="workshop_export",
            entity_type="Workshop",
            entity_id=str(workshop_id),
            metadata={"format": "pdf"},
        )
        return send_file(
            BytesIO(pdf_bytes),
            mimetype="application/pdf",
            download_name=f"{filename_base}.pdf",
            as_attachment=True,
        )

    if export_format == "csv":
        csv_payload = WorkshopAdmin.export_workshop_csv(workshop)
        response = current_app.response_class(csv_payload, mimetype="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename={filename_base}.csv"
        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="workshop_export",
            entity_type="Workshop",
            entity_id=str(workshop_id),
            metadata={"format": "csv"},
        )
        return response

    if export_format == "json":
        json_payload = WorkshopAdmin.export_workshop_data(workshop)
        response = current_app.response_class(
            json.dumps(json_payload, default=str),
            mimetype="application/json",
        )
        response.headers["Content-Disposition"] = f"attachment; filename={filename_base}.json"
        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="workshop_export",
            entity_type="Workshop",
            entity_id=str(workshop_id),
            metadata={"format": "json"},
        )
        return response

    abort(404)


@admin_bp.route("/system", methods=["GET", "POST"])
@login_required
@admin_required
def system_settings():
    form = SystemConfigForm()
    config = ConfigManager.get_environment_config()

    if not form.is_submitted():
        _populate_system_form(form, config)

    if form.validate_on_submit():
        updates: Dict[str, Dict[str, object]] = {
            "app": {
                "name": form.app_name.data,
                "timezone": form.default_timezone.data,
                "workshop_default_duration": form.workshop_default_duration.data,
                "max_workshop_participants": form.max_workshop_participants.data,
                "enable_ai_features": form.enable_ai_features.data,
                "bedrock_model_id": form.bedrock_model_id.data,
            },
            "mail": {
                "server": form.smtp_server.data,
                "port": form.smtp_port.data,
                "username": form.mail_username.data,
            },
        }

        for section, values in updates.items():
            for key, value in values.items():
                ConfigManager.update_config(section, key, value)

        AdminLog.log_action(
            actor_id=current_user.user_id,
            action="config_updated",
            entity_type="Config",
            entity_id="system",
            metadata=updates,
        )
        flash("Configuration updated.", "success")
        return redirect(url_for("admin.system_settings"))

    return render_template(
        "system.html",
        form=form,
        config=config,
        overrides=ConfigManager.load_overrides(),
        health=HealthMonitor.get_system_health(),
    )


@admin_bp.route("/logs")
@login_required
@admin_required
def logs():
    page = request.args.get("page", 1, type=int)
    per_page = 25
    pagination = (
        AdminLog.query.order_by(AdminLog.created_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return render_template(
        "logs.html",
        pagination=pagination,
        logs=pagination.items,
    )