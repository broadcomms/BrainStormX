# app/__init__.py
import os
from typing import Any, DefaultDict, Dict, Tuple

import markdown
from flask import Flask
from flask_cors import CORS
from .config import Config
from sqlalchemy import text
from .extensions import db, socketio, login_manager, mail
from app.utils.session_tracker import touch_user_session
from app.models import User 

USE_LANGGRAPH = os.environ.get("ENABLE_LANGGRAPH", "1") not in {"0", "false", "False"}


"""
Note on import ordering:
We avoid importing blueprints at module import time to prevent circular imports
with sockets re-exports and workshop routes. All blueprints are imported inside
create_app() after Socket.IO handlers are registered.
"""
from flask_login import current_user
from sqlalchemy import func
from .models import WorkspaceMember, Workshop, WorkshopParticipant

# Example assuming static folder is inside 'app' directory
static_dir = os.path.join(os.path.dirname(__file__), 'static')

def create_app(config_filename=None):
    app = Flask(__name__, static_folder=static_dir, static_url_path='/static')
    app.config.from_object(Config)

    # If running under pytest, force an in-memory DB and testing mode BEFORE init_app
    # so the SQLAlchemy engine binds to the correct URI for the lifetime of the app.
    # Also, tests will call db.create_all() explicitly after overriding config as needed.
    if os.environ.get('PYTEST_CURRENT_TEST'):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'

    # Ensure instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # Enable CORS
    CORS(app, resources={r"/*": {"origins": "*"}}) # Allow all for dev, adjust later

    # Initialize Application Extensions
    db.init_app(app)
    login_manager.init_app(app) # Initialize LoginManager
    mail.init_app(app) # Initialize Mail
    # Select Socket.IO async mode.
    # We default to eventlet for broad compatibility, but the Vosk transcription
    # provider spins up its own background threads and dedicated asyncio loops.
    # Running those inside an eventlet-monkey-patched environment can produce
    # 'RuntimeError: Cannot run the event loop while another loop is running'
    # when the greenlet and real thread event loops interleave. For reliability
    # we switch to the 'threading' async_mode whenever Vosk is selected, or
    # during tests. This avoids eventlet's monkey patching of socket / threading
    # primitives used by the provider worker threads.
    async_mode: str = "eventlet"
    provider_env = os.environ.get('TRANSCRIPTION_PROVIDER') or os.environ.get('STT_PROVIDER') or ''
    if provider_env.lower() == 'vosk':
        async_mode = 'threading'
    if os.environ.get('PYTEST_CURRENT_TEST'):
        async_mode = 'threading'
    socketio_kwargs: Dict[str, Any] = {"cors_allowed_origins": "*", "async_mode": async_mode}
    if async_mode == "threading":
        socketio_kwargs["async_handlers"] = False
    socketio.init_app(app, **socketio_kwargs)
    # Register Socket.IO event handlers (core + feature modules)
    from . import sockets_core  # noqa: F401  # registers core socket events
    # Feature gateway modules live under app/sockets/ (directory)
    try:
        from .sockets import transcription_gateway  # noqa: F401
    except Exception:
        pass
    try:
        from .sockets import video_conference_gateway  # noqa: F401
    except Exception as e:  # Make failures visible for tests registering handlers
        app.logger.error(f"Failed to import video_conference_gateway: {e}", exc_info=True)
        raise
    # Register TTS gateway (handles 'tts_request' events)
    try:
        from .sockets import tts_gateway  # noqa: F401
    except Exception as e:
        app.logger.error(f"Failed to import tts_gateway: {e}", exc_info=True)
    try:
        from .sockets import time_heartbeat  # noqa: F401
    except Exception as e:
        app.logger.warning("time_heartbeat_import_failed", extra={"error": str(e)})
        
    # Register assistant namespace if enabled
    from app.assistant.assistant_socket import AssistantNamespace
    socketio.on_namespace(AssistantNamespace('/assistant'))

    # --- Flask-Login Configuration ---
    login_manager.login_view = 'auth_bp.login'  # type: ignore[attr-defined]  # Route name for login page
    login_manager.login_message_category = 'info' # Flash message category

    @login_manager.user_loader
    def load_user(user_id):
        # Return the user object from the user ID stored in the session
        try:
            return db.session.get(User, int(user_id))  # SQLAlchemy 2.0 style
        except Exception:
            return None
    # --------------------------------

    @app.before_request
    def _update_user_session_activity():
        if current_user.is_authenticated:
            touch_user_session(current_user)
    
    # --- Register Jinja Filter for Markdown ---
    @app.template_filter('markdown')
    def markdown_filter(text):
        """Converts Markdown text to HTML."""
        # You can add extensions here if needed, e.g., 'fenced_code', 'tables'
        return markdown.markdown(text, extensions=['fenced_code'])
    # -----------------------------------------

    # Register App Blueprints (import lazily to avoid cycles)
    from .main.routes import main_bp
    from .main.video import video_bp
    from .auth.routes import auth_bp
    from .admin import admin_bp, admin_api_bp
    from .account.routes import account_bp
    from .workspace.routes import workspace_bp
    from .document.routes import document_bp
    from .document.queue import init_scheduler
    from .workshop.routes import workshop_bp

    if USE_LANGGRAPH:
        try:
            from .service.routes.agent import agent_bp  # type: ignore
        except Exception:
            agent_bp = None  # type: ignore
        try:
            from .service.routes.agenda import generate_agenda_text  # noqa: F401  # side effect use
        except Exception:
            pass
    else:
        agent_bp = None  # type: ignore

    from .service.routes.ideas import ideas_bp
    from .service.routes.photo import photo_bp, media_bp
    from .service.routes.report_media import reports_bp
    from .service.routes.tts import tts_bp
    from .service.routes.transcripts import transcripts_bp
    from .service.routes.forum import forum_bp
    from .service.routes.discussion import discussion_bp
    from app.assistant.assistant_controller import bp as assistant_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(video_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(admin_api_bp)
    app.register_blueprint(account_bp, url_prefix="/account")
    app.register_blueprint(workspace_bp, url_prefix="/workspace")
    app.register_blueprint(document_bp, url_prefix="/document")
    app.register_blueprint(workshop_bp, url_prefix="/workshop")
    app.register_blueprint(assistant_bp)
    if agent_bp is not None:  # Only register if available / enabled
        app.register_blueprint(agent_bp, url_prefix="/agent")  # Register agent blueprint
    app.register_blueprint(ideas_bp, url_prefix="/ideas")
    app.register_blueprint(photo_bp, url_prefix="/photo")
    app.register_blueprint(media_bp)  # serves /media/photos/*
    app.register_blueprint(tts_bp)
    app.register_blueprint(reports_bp)  # serves /media/reports/*
    app.register_blueprint(transcripts_bp, url_prefix="")  # exposes /api/workshops/... endpoints
    app.register_blueprint(forum_bp, url_prefix="")  # exposes /api/workshops/.../forum endpoints
    app.register_blueprint(discussion_bp, url_prefix="")
    try:
        from app.assistant.tools.metric import metrics_bp

        app.register_blueprint(metrics_bp)
    except Exception as exc:  # pragma: no cover - metrics optional
        app.logger.warning("metrics_blueprint_not_registered", extra={"error": str(exc)})

    # Initialize document processing scheduler now that extensions and blueprints are ready
    init_scheduler(app)

    with app.app_context():
        # Create database tables if they don't exist. Under pytest we force an
        # in-memory SQLite DB, so this is safe and ensures tests that don't
        # explicitly call db.create_all() still have the schema available.
        db.create_all()

        # Lightweight SQLite-only schema migration to add newly introduced columns
        # without Alembic. This is safe to run multiple times and only applies if
        # the columns are missing.
        try:
            if db.engine.url.get_backend_name() == 'sqlite':
                conn = db.session
                # helper to check if column exists on a table
                def _has_column(table: str, column: str) -> bool:
                    rows = conn.execute(text(f"PRAGMA table_info({table})"))
                    for r in rows:
                        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
                        if str(r[1]).lower() == column.lower():
                            return True
                    return False

                # forum_topics: pinned (INTEGER NOT NULL DEFAULT 0), locked (INTEGER NOT NULL DEFAULT 0)
                if _has_column('forum_topics', 'id'):
                    if not _has_column('forum_topics', 'pinned'):
                        conn.execute(text("ALTER TABLE forum_topics ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"))
                    if not _has_column('forum_topics', 'locked'):
                        conn.execute(text("ALTER TABLE forum_topics ADD COLUMN locked INTEGER NOT NULL DEFAULT 0"))

                # forum_posts: edited_at (DATETIME NULL)
                if _has_column('forum_posts', 'id') and not _has_column('forum_posts', 'edited_at'):
                    conn.execute(text("ALTER TABLE forum_posts ADD COLUMN edited_at DATETIME NULL"))

                # forum_replies: edited_at (DATETIME NULL)
                if _has_column('forum_replies', 'id') and not _has_column('forum_replies', 'edited_at'):
                    conn.execute(text("ALTER TABLE forum_replies ADD COLUMN edited_at DATETIME NULL"))

                # captured_decisions metadata columns
                if _has_column('captured_decisions', 'id'):
                    if not _has_column('captured_decisions', 'status'):
                        conn.execute(text("ALTER TABLE captured_decisions ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'draft'"))
                    if not _has_column('captured_decisions', 'confirmed_at'):
                        conn.execute(text("ALTER TABLE captured_decisions ADD COLUMN confirmed_at DATETIME NULL"))
                    if not _has_column('captured_decisions', 'confirmed_by_user_id'):
                        conn.execute(text("ALTER TABLE captured_decisions ADD COLUMN confirmed_by_user_id INTEGER NULL"))

                # discussion_notes provenance column
                if _has_column('discussion_notes', 'id') and not _has_column('discussion_notes', 'origin'):
                    conn.execute(text("ALTER TABLE discussion_notes ADD COLUMN origin VARCHAR(32) NOT NULL DEFAULT 'chat'"))

                # documents table foundational columns
                if _has_column('documents', 'id'):
                    column_defs = {
                        'description': "ALTER TABLE documents ADD COLUMN description TEXT",
                        'content': "ALTER TABLE documents ADD COLUMN content TEXT",
                        'version': "ALTER TABLE documents ADD COLUMN version INTEGER DEFAULT 1",
                        'parent_document_id': "ALTER TABLE documents ADD COLUMN parent_document_id INTEGER",
                        'is_archived': "ALTER TABLE documents ADD COLUMN is_archived BOOLEAN DEFAULT 0",
                        'archived_at': "ALTER TABLE documents ADD COLUMN archived_at DATETIME",
                        'last_accessed_at': "ALTER TABLE documents ADD COLUMN last_accessed_at DATETIME",
                        'access_count': "ALTER TABLE documents ADD COLUMN access_count INTEGER DEFAULT 0",
                        'summary': "ALTER TABLE documents ADD COLUMN summary TEXT",
                        'markdown': "ALTER TABLE documents ADD COLUMN markdown TEXT",
                        'tts_script': "ALTER TABLE documents ADD COLUMN tts_script TEXT",
                        'processing_status': "ALTER TABLE documents ADD COLUMN processing_status VARCHAR(50) DEFAULT 'pending'",
                        'last_processed_at': "ALTER TABLE documents ADD COLUMN last_processed_at DATETIME",
                        'content_sha256': "ALTER TABLE documents ADD COLUMN content_sha256 VARCHAR(64)",
                        'processing_attempts': "ALTER TABLE documents ADD COLUMN processing_attempts INTEGER DEFAULT 0",
                        'processing_started_at': "ALTER TABLE documents ADD COLUMN processing_started_at DATETIME",
                    }
                    for column, statement in column_defs.items():
                        if not _has_column('documents', column):
                            conn.execute(text(statement))

                # document_processing_jobs table bootstrap columns
                if _has_column('document_processing_jobs', 'id'):
                    job_column_defs = {
                        'job_type': "ALTER TABLE document_processing_jobs ADD COLUMN job_type VARCHAR(100) DEFAULT 'document_full'",
                        'status': "ALTER TABLE document_processing_jobs ADD COLUMN status VARCHAR(50) DEFAULT 'pending'",
                        'priority': "ALTER TABLE document_processing_jobs ADD COLUMN priority INTEGER DEFAULT 10",
                        'created_at': "ALTER TABLE document_processing_jobs ADD COLUMN created_at DATETIME",
                        'started_at': "ALTER TABLE document_processing_jobs ADD COLUMN started_at DATETIME",
                        'completed_at': "ALTER TABLE document_processing_jobs ADD COLUMN completed_at DATETIME",
                        'error_message': "ALTER TABLE document_processing_jobs ADD COLUMN error_message TEXT",
                        'attempts': "ALTER TABLE document_processing_jobs ADD COLUMN attempts INTEGER DEFAULT 0",
                    }
                    for column, statement in job_column_defs.items():
                        if not _has_column('document_processing_jobs', column):
                            conn.execute(text(statement))

                # document_processing_logs table bootstrap columns
                if _has_column('document_processing_logs', 'id'):
                    log_column_defs = {
                        'started_at': "ALTER TABLE document_processing_logs ADD COLUMN started_at DATETIME",
                        'completed_at': "ALTER TABLE document_processing_logs ADD COLUMN completed_at DATETIME",
                        'stage': "ALTER TABLE document_processing_logs ADD COLUMN stage VARCHAR(100)",
                        'status': "ALTER TABLE document_processing_logs ADD COLUMN status VARCHAR(50) DEFAULT 'pending'",
                        'error_message': "ALTER TABLE document_processing_logs ADD COLUMN error_message TEXT",
                        'processed_pages': "ALTER TABLE document_processing_logs ADD COLUMN processed_pages INTEGER",
                        'total_pages': "ALTER TABLE document_processing_logs ADD COLUMN total_pages INTEGER",
                        'created_at': "ALTER TABLE document_processing_logs ADD COLUMN created_at DATETIME",
                    }
                    for column, statement in log_column_defs.items():
                        if not _has_column('document_processing_logs', column):
                            conn.execute(text(statement))

                if _has_column('workshops', 'id'):
                    workshop_column_defs = {
                        'current_phase': "ALTER TABLE workshops ADD COLUMN current_phase VARCHAR(128)",
                        'agenda_json': "ALTER TABLE workshops ADD COLUMN agenda_json TEXT",
                        'agenda_generated_at': "ALTER TABLE workshops ADD COLUMN agenda_generated_at DATETIME",
                        'agenda_generated_source': "ALTER TABLE workshops ADD COLUMN agenda_generated_source VARCHAR(32)",
                        'agenda_auto_generate': "ALTER TABLE workshops ADD COLUMN agenda_auto_generate BOOLEAN NOT NULL DEFAULT 1",
                        'agenda_draft_plaintext': "ALTER TABLE workshops ADD COLUMN agenda_draft_plaintext TEXT",
                        'facilitator_guidelines': "ALTER TABLE workshops ADD COLUMN facilitator_guidelines TEXT",
                        'facilitator_tips': "ALTER TABLE workshops ADD COLUMN facilitator_tips TEXT",
                        'facilitator_summary': "ALTER TABLE workshops ADD COLUMN facilitator_summary TEXT",
                        'agenda_confidence': "ALTER TABLE workshops ADD COLUMN agenda_confidence VARCHAR(16)"
                    }
                    for column, statement in workshop_column_defs.items():
                        if not _has_column('workshops', column):
                            conn.execute(text(statement))

                if _has_column('workshop_agenda', 'id'):
                    agenda_column_defs = {
                        'task_type': "ALTER TABLE workshop_agenda ADD COLUMN task_type VARCHAR(64)",
                        'origin': "ALTER TABLE workshop_agenda ADD COLUMN origin VARCHAR(16) NOT NULL DEFAULT 'organizer'",
                        'duration_minutes': "ALTER TABLE workshop_agenda ADD COLUMN duration_minutes INTEGER"
                    }
                    for column, statement in agenda_column_defs.items():
                        if not _has_column('workshop_agenda', column):
                            conn.execute(text(statement))
                # chat_threads: deleted_at (DATETIME NULL), created_by_id index may already exist via model
                if _has_column('chat_threads', 'id') and not _has_column('chat_threads', 'deleted_at'):
                    conn.execute(text("ALTER TABLE chat_threads ADD COLUMN deleted_at DATETIME NULL"))
                try:
                    conn.commit()
                except Exception:
                    pass
        except Exception:
            # Never block app startup due to best-effort migration
            pass
        # Ensure media directories exist
        from .config import Config as _Cfg
        try:
            os.makedirs(_Cfg.MEDIA_PHOTOS_DIR, exist_ok=True)
        except Exception:
            pass
    # --- Testing Diagnostics: capture registered socket handlers ---
    if app.config.get('TESTING'):
        names = set()
        try:
            # Prefer inspecting the underlying python-socketio server if available
            srv = getattr(socketio, 'server', None)
            handler_dict = None
            if srv is not None:
                handler_dict = getattr(srv, 'handlers', None)
            if not isinstance(handler_dict, dict):
                # Fallback to Flask-SocketIO wrapper attribute if present
                handler_dict = getattr(socketio, 'handlers', {})
            if isinstance(handler_dict, dict):
                root_ns = handler_dict.get('/', {})
                if isinstance(root_ns, dict):
                    for evt in root_ns.keys():
                        names.add(evt)
        except Exception:
            pass
        # Final defensive fallback: ensure the interactive voting event is visible if handler imported
        try:
            from .sockets_core.core import _on_submit_vote_generic  # noqa: F401
            names.add('submit_vote_generic')
        except Exception:
            pass
        app.config['SOCKETIO_HANDLER_NAMES'] = sorted(list(names))
    
    # ---- Global Jinja helper to resolve profile image URLs ----
    @app.context_processor
    def inject_profile_url_helper():
        from .config import Config as _Cfg
        def profile_url(path_or_none):
            """Return a safe URL for profile images.
            - If None/empty: return default static image
            - If absolute http(s): return as-is
            - If startswith('/media/'): return as-is (served by media_bp)
            - If startswith('/static/'): return as-is
            - Else: treat as a static file path under /static
            """
            default_path = 'images/default-profile.png'
            p = (path_or_none or '').strip()
            if not p:
                return f"/static/{default_path}"
            if p.startswith('http://') or p.startswith('https://'):
                return p
            if p.startswith('/media/') or p.startswith('/static/'):
                return p
            # Allow media config prefix without leading slash
            if p.startswith(_Cfg.MEDIA_PHOTOS_URL_PREFIX.strip('/')):
                return '/' + p
            # Otherwise assume it's a relative static file path
            return f"/static/{p}"
        return dict(profile_url=profile_url)
    
    # ---- Context Processor: basic user metrics for navbar ----
    @app.context_processor
    def inject_user_metrics():
        """Inject lightweight nav badge metrics (avoid heavy queries)."""
        if current_user.is_authenticated:
            # Workspaces: active memberships
            workspace_count = WorkspaceMember.query.filter_by(
                user_id=current_user.user_id, status='active'
            ).count()

            # Workshops: user created OR participates in (distinct)
            try:
                from sqlalchemy import or_  # local import to avoid shadowing
                workshop_count = (
                    db.session.query(func.count(func.distinct(Workshop.id)))
                    .select_from(Workshop)
                    .outerjoin(
                        WorkshopParticipant,
                        WorkshopParticipant.workshop_id == Workshop.id,
                    )
                    .filter(
                        or_(
                            Workshop.created_by_id == current_user.user_id,
                            WorkshopParticipant.user_id == current_user.user_id,
                        )
                    )
                    .scalar()
                ) or 0
            except Exception:
                workshop_count = Workshop.query.filter(
                    Workshop.created_by_id == current_user.user_id
                ).count()

            # Documents: across user's active workspaces
            try:
                from .models import Document, Workspace  # local import to avoid cycles
                active_workspace_ids = [
                    wm.workspace_id
                    for wm in WorkspaceMember.query.filter_by(
                        user_id=current_user.user_id, status='active'
                    ).all()
                ]
                if active_workspace_ids:
                    document_count = (
                        Document.query.filter(
                            Document.workspace_id.in_(active_workspace_ids)
                        ).count()
                    )
                else:
                    document_count = 0
            except Exception:
                document_count = 0

            # People: public profiles (exclude self if public)
            try:
                public_count = User.query.filter_by(is_public_profile=True).count()
                if getattr(current_user, "is_public_profile", False):
                    people_count = max(0, public_count - 1)
                else:
                    people_count = public_count
            except Exception:
                people_count = 0

            # Ideas: count of finalist (representative) ideas shown on Ideas page
            try:
                from .models import BrainstormIdea, BrainstormTask, IdeaVote, IdeaCluster
                # Workspaces the user can access
                active_workspace_ids = [
                    wm.workspace_id
                    for wm in WorkspaceMember.query.filter_by(
                        user_id=current_user.user_id, status='active'
                    ).all()
                ]
                if not active_workspace_ids:
                    ideas_count = 0
                else:
                    # Base ideas in completed workshops the user can access
                    base_ideas = (
                        BrainstormIdea.query
                        .join(BrainstormTask, BrainstormIdea.task_id == BrainstormTask.id)
                        .join(Workshop, BrainstormTask.workshop_id == Workshop.id)
                        .filter(
                            Workshop.workspace_id.in_(active_workspace_ids),
                            Workshop.status == 'completed',
                        )
                        .all()
                    )
                    if not base_ideas:
                        ideas_count = 0
                    else:
                        # Collect cluster and workshop ids from ideas
                        cluster_ids = [i.cluster_id for i in base_ideas if getattr(i, 'cluster_id', None)]
                        # Map idea by (workshop_id, cluster_id) to earliest idea id
                        from collections import defaultdict
                        earliest_by_ws_cluster: Dict[Tuple[int, int], Tuple[int, Any]] = {}
                        for i in base_ideas:
                            task = getattr(i, 'task', None)
                            ws = getattr(task, 'workshop', None) if task else None
                            cid = getattr(i, 'cluster_id', None)
                            if not ws or not cid:
                                continue
                            key = (ws.id, cid)
                            prev = earliest_by_ws_cluster.get(key)
                            if prev is None or i.timestamp < prev[1]:
                                earliest_by_ws_cluster[key] = (i.id, i.timestamp)

                        # Count votes per cluster among those clusters
                        vote_counts: Dict[int, int] = {}
                        if cluster_ids:
                            rows = (
                                db.session.query(IdeaVote.cluster_id, func.count(IdeaVote.id))
                                .filter(IdeaVote.cluster_id.in_(cluster_ids))
                                .group_by(IdeaVote.cluster_id)
                                .all()
                            )
                            vote_counts = {cid: cnt for cid, cnt in rows}

                        # Determine winning clusters per workshop (max votes) and count representatives
                        per_ws_cluster_votes: DefaultDict[int, Dict[int, int]] = defaultdict(dict)
                        for (ws_id, cid), (iid, _) in earliest_by_ws_cluster.items():
                            per_ws_cluster_votes[ws_id][cid] = vote_counts.get(cid, 0)

                        representative_ids: set[int] = set()
                        for ws_id, cl_map in per_ws_cluster_votes.items():
                            if not cl_map:
                                continue
                            max_votes = max(cl_map.values())
                            winners = {cid for cid, v in cl_map.items() if v == max_votes}
                            for cid in winners:
                                rep = earliest_by_ws_cluster.get((ws_id, cid))
                                if rep:
                                    representative_ids.add(rep[0])

                        ideas_count = len(representative_ids)
            except Exception:
                ideas_count = 0
        else:
            workspace_count = workshop_count = document_count = people_count = ideas_count = 0

        return dict(
            nav_workspace_count=workspace_count,
            nav_workshop_count=workshop_count,
            nav_document_count=document_count,
            nav_people_count=people_count,
            nav_idea_count=ideas_count,
        )

    return app
