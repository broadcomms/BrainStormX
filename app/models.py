# app/models.py
from datetime import datetime, timedelta
from flask_login import UserMixin
from sqlalchemy import JSON, event, text
from sqlalchemy.orm import foreign
from sqlalchemy.types import PickleType, TypeDecorator
from .extensions import db
import secrets # Added for participants token
import json # Added for whiteboard content

try:
    from pgvector.sqlalchemy import Vector  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    class Vector(TypeDecorator):  # type: ignore
        """Fallback vector type that stores data via Pickle when pgvector isn't available."""

        impl = PickleType
        cache_ok = True

        def __init__(self, dimensions: int, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.dimensions = dimensions

        def process_bind_param(self, value, dialect):
            if value is None:
                return None
            if isinstance(value, (list, tuple)):
                return list(value)
            return value

        def process_result_value(self, value, dialect):
            return value

# ---------------- User Model ----------------
class User(db.Model, UserMixin):
    __tablename__ = "users"
    user_id = db.Column(db.Integer, primary_key=True)

    # Basic Identity
    username = db.Column(db.String(100), nullable=True)  # Used for display name
    
    email = db.Column(db.String(255), unique=True, nullable=False)  # For login
    password = db.Column(db.Text, nullable=False)

    # Profile Details
    first_name = db.Column(db.String(100), nullable=True)
    last_name = db.Column(db.String(100), nullable=True)
    job_title = db.Column(db.String(150), nullable=True)
    phone_number = db.Column(db.String(50), nullable=True)
    organization = db.Column(db.String(150), nullable=True)
    # Role Based Access Control (RBAC)
    role = db.Column(db.String(50), default="user")  # 'admin', 'manager', 'user'

    # Email verification
    email_verified = db.Column(db.Boolean, default=False)
    verification_token = db.Column(db.String(255), nullable=True)

    # Password Reset
    reset_token = db.Column(db.String(255), nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)

    # Profile Picture
    profile_pic_url = db.Column(db.String(255), default="images/default-profile.png")

    # Profile Visibility (public profiles appear in People directory)
    is_public_profile = db.Column(db.Boolean, default=True, nullable=False)
    # NOTE: Run a database migration or manual ALTER TABLE to add this column in existing deployments:
    # ALTER TABLE users ADD COLUMN is_public_profile BOOLEAN NOT NULL DEFAULT 1;

    # Timestamps
    created_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    updated_timestamp = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    workspace_memberships = db.relationship("WorkspaceMember", back_populates="user", lazy='dynamic')
    uploaded_documents = db.relationship("Document", back_populates="uploader", lazy='dynamic')
    created_workshops = db.relationship("Workshop", back_populates="creator", foreign_keys="Workshop.created_by_id", lazy='dynamic')
    workshop_participations = db.relationship("WorkshopParticipant", back_populates="user", lazy='dynamic')

    def get_id(self):
        return str(self.user_id)

    # Convenient unified display name (falls back gracefully)
    @property
    def display_name(self) -> str:
        parts = [p for p in [self.first_name, self.last_name] if p]
        if parts:
            return " ".join(parts)
        if self.username:
            return self.username
        if self.email:
            return (self.email.split('@')[0])
        return f"User{self.user_id}"


# ---------------- Workspace Model ----------------
class Workspace(db.Model):
    __tablename__ = "workspaces"
    workspace_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    is_private = db.Column(db.Boolean, default=True)
    created_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    updated_timestamp = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    description = db.Column(db.Text, nullable=True)
    logo_url = db.Column(db.String(255), default="")

    tip = db.Column(db.Text, nullable=True) # JSON or text representation of generated tips
    current_task_id = db.Column(db.Integer, nullable=True) # Link to the currently active task (denormalized pointer)
    timer_start_time = db.Column(db.DateTime, nullable=True) # When the current active period started (start or resume)
    timer_paused_at = db.Column(db.DateTime, nullable=True) # Timestamp of the last pause
    timer_elapsed_before_pause = db.Column(db.Integer, default=0) # Seconds elapsed before the last pause
    task_sequence = db.Column(db.Text, nullable=True) # Store the sequence of tasks (e.g., from action plan)
    current_task_index = db.Column(db.Integer, nullable=True, default=None) # Index within task_sequence
    auto_advance_enabled = db.Column(db.Boolean, default=True)  # Whether to auto-advance after a short delay when a task ends
    auto_advance_after_seconds = db.Column(db.Integer, default=8, nullable=True)  # Delay before advancing
    owner = db.relationship("User", backref=db.backref("owned_workspaces", lazy=True))
    members = db.relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan", lazy='selectin')
    documents = db.relationship("Document", back_populates="workspace", cascade="all, delete-orphan", lazy='dynamic')
    workshops = db.relationship("Workshop", back_populates="workspace", cascade="all, delete-orphan", lazy='dynamic')


# ------------- Workspace Member Model ----------------
class WorkspaceMember(db.Model):
    __tablename__ = "workspace_members"
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    role = db.Column(db.String(50), default="member")  # RBAC: 'admin', 'member', 'viewer'
    status = db.Column(db.String(50), default="active")  # STATUS: 'active', 'invited','declined','inactive', 'requested'
    joined_timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    user = db.relationship("User", back_populates="workspace_memberships")
    workspace = db.relationship("Workspace", back_populates="members")

    # Unique constraint
    __table_args__ = (db.UniqueConstraint('workspace_id', 'user_id', name='_workspace_user_uc'),)


# ---------------- Member Invitation Model ----------------
class Invitation(db.Model):
    __tablename__ = "invitations"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(255), unique=True, nullable=False)
    email = db.Column(db.String(255), nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=False)
    inviter_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    sent_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    expiration_timestamp = db.Column(db.DateTime)
    custom_message = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='pending', nullable=False) #STATUS: 'pending', 'accepted', 'declined', 'expired'

    # Relationships
    workspace = db.relationship("Workspace", backref=db.backref("invitations", lazy=True))
    inviter   = db.relationship("User",      backref=db.backref("sent_invitations", lazy=True))

    # Helper method to generate token and set expiration ---
    def generate_token(self, expires_in_days=7):
        self.token = secrets.token_urlsafe(32)
        self.expiration_timestamp = datetime.utcnow() + timedelta(days=expires_in_days)

    # Helper method to check if token is valid ---
    def is_valid(self):
        return self.status == 'pending' and self.expiration_timestamp and self.expiration_timestamp > datetime.utcnow()


# ---------------- Document Model ----------------
class Document(db.Model):
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=False)
    
    title = db.Column(db.String(255), nullable=False)
    file_name = db.Column(db.String(255), nullable=False) # Original uploaded filename
    file_path = db.Column(db.String(255), nullable=False) # Path relative to instance/uploads
    
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    file_size = db.Column(db.Integer, nullable=True) # Store file size in bytes
    description = db.Column(db.Text, nullable=True) # <-- ADDED THIS FIELD
    
    content = db.Column(db.Text, nullable=True)  # Document text content
    version = db.Column(db.Integer, default=1)  # Document version number
    parent_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=True)  # For versioning
    is_archived = db.Column(db.Boolean, default=False)  # Archive status
    archived_at = db.Column(db.DateTime, nullable=True)  # Timestamp when archived
    last_accessed_at = db.Column(db.DateTime, nullable=True)  # Last access timestamp
    access_count = db.Column(db.Integer, default=0)  # Number of times accessed
    
    summary = db.Column(db.Text, nullable=True)  # Auto-generated summary of the document
    markdown = db.Column(db.Text, nullable=True)  # Markdown representation of the document content
    tts_script = db.Column(db.Text, nullable=True)  # TTS script generated from the document
    processing_status = db.Column(db.String(50), default='pending')  # idle, queued, 'processing', 'completed', 'failed'
    last_processed_at = db.Column(db.DateTime, nullable=True)  # Timestamp of last processing attempt
    content_sha256 = db.Column(db.String(64), nullable=True)  # SHA-256 hash of the content for change detection
    processing_attempts = db.Column(db.Integer, default=0)  # Number of processing attempts
    processing_started_at = db.Column(db.DateTime, nullable=True)  # Timestamp when processing started
    
    chunks = db.relationship("Chunk", back_populates="document", cascade="all, delete-orphan")
    images = db.relationship("Image", back_populates="document", cascade="all, delete-orphan")
    
    # Relationships
    uploader = db.relationship("User", back_populates="uploaded_documents")
    workspace = db.relationship("Workspace", back_populates="documents")
    workshop_links = db.relationship("WorkshopDocument", back_populates="document", cascade="all, delete-orphan", lazy='dynamic')

    # Parent-child relationship for document versioning
    parent_document = db.relationship("Document", remote_side=[id], backref=db.backref("child_documents", lazy=True))

    @property
    def chunks_with_embeddings_count(self):
        """Count the number of chunks that have embeddings."""
        try:
            chunks_iterable = list(self.chunks)  # type: ignore[arg-type]
        except TypeError:
            chunks_iterable = []
        return sum(1 for chunk in chunks_iterable if getattr(chunk, "vector", None) is not None)
    
    @property 
    def has_content_processed(self):
        """Check if document content has been processed into chunks."""
        try:
            chunks_iterable = list(self.chunks)  # type: ignore[arg-type]
        except TypeError:
            chunks_iterable = []
        return self.content is not None and any(chunks_iterable)

    def increment_access_count(self):
        """Increment the access count and update last accessed time."""
        self.access_count = (self.access_count or 0) + 1
        self.last_accessed_at = datetime.utcnow()
    
    def archive(self):
        """Archive the document."""
        self.is_archived = True
        self.archived_at = datetime.utcnow()
    
    def unarchive(self):
        """Unarchive the document."""
        self.is_archived = False
        self.archived_at = None
        
# ---------------- Document Chunk Model ----------------
class Chunk(db.Model):
    __tablename__ = "document_chunks"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("documents.id"), nullable=False
    )
    content = db.Column(db.Text, nullable=False)
    vector = db.Column(Vector(384), nullable=True)  # Vector for similarity search (384 dims for sentence-transformers)
    meta_data = db.Column(JSON, nullable=True)  # Store additional metadata as JSON
    # Relationships
    document = db.relationship("Document", back_populates="chunks")

# ---------------- Docoment Image Model ----------------
class Image(db.Model):
    __tablename__ = "document_images"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("documents.id"), nullable=False
    )
    image_url = db.Column(db.String(255), nullable=False)  # URL to the image file
    caption = db.Column(db.String(255), nullable=True)  # Optional caption for the image

    # Relationships
    document = db.relationship("Document", back_populates="images")
    
# ---------------- Workshop Document Audios ----------------
class DocumentAudio(db.Model):  
    __tablename__ = "document_audios"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    audio_file_path = db.Column(db.String(255), nullable=False)  # Path to the audio file
    duration_seconds = db.Column(db.Integer, nullable=True)  # Duration of the audio in seconds
    audio_sha256 = db.Column(db.String(64), nullable=True)  # SHA-256 hash of the audio file for integrity
    storage_backend = db.Column(db.String(50), nullable=True)  # e.g., 'session','local', 's3', 'gcs'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    document = db.relationship("Document", backref=db.backref("audios", lazy=True))
    
    
# ---------------- Document Processing Job Queue ----------------

class DocumentProcessingJob(db.Model):
    __tablename__ = "document_processing_jobs"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    job_type = db.Column(db.String(100), nullable=False)  # e.g., 'text_extraction', 'chunking', 'embedding'
    status = db.Column(db.String(50), default='pending')  # 'pending', 'in_progress', 'completed', 'failed'
    priority = db.Column(db.Integer, default=10)  # Lower number = higher priority
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)  # Store error message if job failed
    attempts = db.Column(db.Integer, default=0)  # Number of processing attempts

    # Relationships
    document = db.relationship("Document", backref=db.backref("processing_jobs", lazy=True))
    
# ---------------- Document Processing Log ----------------
class DocumentProcessingLog(db.Model):
    __tablename__ = "document_processing_logs"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    stage = db.Column(db.String(100), nullable=True)  # e.g., 'upload', 'text_extraction', 'chunking', 'embedding'
    status = db.Column(db.String(50), default='pending')  # 'pending', 'processing', 'completed', 'failed'
    error_message = db.Column(db.Text, nullable=True)  # Store error message if processing failed
    processed_pages = db.Column(db.Integer, nullable=True)  # Number of pages processed
    total_pages = db.Column(db.Integer, nullable=True)  # Total number of pages in the document
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    document = db.relationship("Document", backref=db.backref("processing_logs", lazy=True))


class DocumentProcessingLogArchive(db.Model):
    __tablename__ = "document_processing_log_archives"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=True, index=True)
    stage = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    processed_pages = db.Column(db.Integer, nullable=True)
    total_pages = db.Column(db.Integer, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    archived_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    archived_by_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True)
    source_log_id = db.Column(db.Integer, nullable=True)

    archived_by = db.relationship("User", backref=db.backref("archived_processing_logs", lazy=True))

# ---------------- Workshop Model ----------------
class Workshop(db.Model):
    __tablename__ = "workshops"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    objective = db.Column(db.Text, nullable=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=False)
    date_time = db.Column(db.DateTime, nullable=False)
    duration = db.Column(db.Integer, nullable=True) # Duration in minutes
    status = db.Column(db.String(50), default="scheduled") #STATUS: 'scheduled', 'inprogress', 'paused', 'completed', 'cancelled'
    agenda = db.Column(db.Text, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    rules = db.Column(db.Text, nullable=True) # JSON or text representation of generated rules
    icebreaker = db.Column(db.Text, nullable=True) # JSON or text representation of generated icebreaker
    tip = db.Column(db.Text, nullable=True) # JSON or text representation of generated tips
    
    # --- MODIFIED/ADDED FOR PERSISTENCE ---
    # Link to the currently active task
    current_phase = db.Column(db.String(128), nullable=True)
    current_task_id = db.Column(db.Integer, nullable=True)
    phase_started_at = db.Column(db.DateTime, nullable=True)
    # NOTE: For existing deployments run: ALTER TABLE workshops ADD COLUMN phase_started_at TIMESTAMP NULL;

    # Timer state tracking
    timer_start_time = db.Column(db.DateTime, nullable=True) # When the current active period started (start or resume)
    timer_paused_at = db.Column(db.DateTime, nullable=True) # Timestamp of the last pause
    timer_elapsed_before_pause = db.Column(db.Integer, default=0) # Seconds elapsed before the last pause

    # Store the sequence of tasks (e.g., from action plan) - Keep this if used for task generation
    task_sequence = db.Column(db.Text, nullable=True)
    current_task_index = db.Column(db.Integer, nullable=True, default=None) # Index within task_sequence

    # Auto-advance configuration (per-workshop)
    auto_advance_enabled = db.Column(db.Boolean, default=True)  # Whether to auto-advance after a short delay when a task ends
    auto_advance_after_seconds = db.Column(db.Integer, default=8, nullable=True)  # Delay before advancing
    # --- NEW REAL-TIME COLLAB FEATURES FLAGS ---
    conference_active = db.Column(db.Boolean, default=True, nullable=False)  # Video conference enabled for this workshop
    transcription_enabled = db.Column(db.Boolean, default=True, nullable=False)  # Live STT transcription enabled
    participant_can_delete_transcripts = db.Column(db.Boolean, default=True, nullable=False)  # Allow participants to delete their own transcript lines
    # NOTE: Run a database migration or manual ALTER TABLE in existing deployments, e.g.:
    # ALTER TABLE workshops ADD COLUMN auto_advance_enabled BOOLEAN DEFAULT 1;
    # ALTER TABLE workshops ADD COLUMN auto_advance_after_seconds INTEGER DEFAULT 8;
    # --- NEW: Text-to-Speech defaults (provider/voice/speed) ---
    # Default provider used for TTS playback in the room (e.g., 'piper', 'polly')
    tts_provider = db.Column(db.String(32), nullable=True)
    # Default voice or model identifier/path depending on provider
    tts_voice = db.Column(db.String(128), nullable=True)
    # Default playback speed multiplier (1.0 normal). Providers clamp as needed.
    tts_speed_default = db.Column(db.Float, nullable=True)
    # Auto-read current task's TTS script when the task loads (client will respect this)
    tts_autoread_enabled = db.Column(db.Boolean, default=True, nullable=False)
    # Voting configuration
    dots_per_user = db.Column(db.Integer, default=5, nullable=False)
    # Workshop type/template: 'brainstorm' | 'meeting' | 'presentation' | 'custom'
    type = db.Column(db.String(32), nullable=True, default='brainstorm')
    # --- NEW AGENDA PIPELINE FIELDS ---
    agenda_json = db.Column(db.Text, nullable=True)
    agenda_generated_at = db.Column(db.DateTime, nullable=True)
    agenda_generated_source = db.Column(db.String(32), nullable=True)
    agenda_auto_generate = db.Column(db.Boolean, nullable=False, default=True)
    agenda_draft_plaintext = db.Column(db.Text, nullable=True)
    facilitator_guidelines = db.Column(db.Text, nullable=True)
    facilitator_tips = db.Column(db.Text, nullable=True)
    facilitator_summary = db.Column(db.Text, nullable=True)
    agenda_confidence = db.Column(db.String(16), nullable=True)
    # NOTE: For existing deployments, run migrations or manual DDL such as:
    # ALTER TABLE workshops ADD COLUMN tts_provider VARCHAR(32) NULL;
    # ALTER TABLE workshops ADD COLUMN tts_voice VARCHAR(128) NULL;
    # ALTER TABLE workshops ADD COLUMN tts_speed_default FLOAT NULL;
    # ALTER TABLE workshops ADD COLUMN tts_autoread_enabled BOOLEAN NOT NULL DEFAULT 1;
    # ALTER TABLE workshops ADD COLUMN dots_per_user INTEGER NOT NULL DEFAULT 5;

    # Whiteboard content (optional, alternative is querying ideas)
    # whiteboard_content = db.Column(db.Text, nullable=True) # Example: Store as JSON string
    # --- END MODIFIED/ADDED FOR PERSISTENCE ---

    # Relationships
    tasks = db.relationship(
        "BrainstormTask",
        back_populates="workshop",
        cascade="all, delete-orphan",
        lazy='select',
        # Explicitly state the foreign key column(s) in the *child* table (BrainstormTask)
        # that link back to *this* parent table (Workshop).
        foreign_keys="BrainstormTask.workshop_id"
    )
    workspace = db.relationship("Workspace", back_populates="workshops")
    creator = db.relationship("User", back_populates="created_workshops", foreign_keys=[created_by_id])
    participants = db.relationship("WorkshopParticipant", back_populates="workshop", cascade="all, delete-orphan", lazy='dynamic')
    linked_documents = db.relationship("WorkshopDocument", back_populates="workshop", cascade="all, delete-orphan", lazy='dynamic')
    chat_messages = db.relationship("ChatMessage", back_populates="workshop", cascade="all, delete-orphan", lazy='dynamic', order_by="ChatMessage.timestamp")
    # Normalized session plan items (DB-backed instead of JSON). Order by index.
    plan_items = db.relationship(
        "WorkshopPlanItem",
        back_populates="workshop",
        cascade="all, delete-orphan",
        order_by="WorkshopPlanItem.order_index",
        lazy='selectin'
    )

    # New: normalized agenda rows (source of truth for agenda rendering)
    agenda_items = db.relationship(
        "WorkshopAgenda",
        back_populates="workshop",
        cascade="all, delete-orphan",
        order_by="WorkshopAgenda.position",
        lazy='selectin'
    )

    # Relationship to the current task object
    current_task = db.relationship(
        "BrainstormTask",
        primaryjoin="foreign(Workshop.current_task_id) == BrainstormTask.id",
        lazy="select",
        viewonly=True,
    )

    # Helper property to get the organizer
    @property
    def organizer(self):
        # Assuming organizer is always the creator for simplicity now
        return self.creator
        # Alternative if using role:
        # organizer_participant = self.participants.filter_by(role='organizer').first()
        # return organizer_participant.user if organizer_participant else None

    # --- ADDED: Helper to get remaining time ---
    def get_remaining_task_time(self) -> int:
        """Calculates remaining seconds for the current task, returns 0 if no task/timer."""
        if not self.current_task or not self.current_task.duration:
            return 0

        if self.status == 'paused' and self.timer_paused_at:
            # If paused, remaining time is total duration minus what elapsed before pause
            total_elapsed = self.timer_elapsed_before_pause
        elif self.status == 'inprogress' and self.timer_start_time:
            # If running, calculate elapsed time in current run + time before pause
            elapsed_this_run = (datetime.utcnow() - self.timer_start_time).total_seconds()
            total_elapsed = self.timer_elapsed_before_pause + elapsed_this_run
        else:
            # No timer running or invalid state
            return 0

        remaining = self.current_task.duration - total_elapsed
        return max(0, int(remaining)) # Return non-negative integer


# ---------------- Workshop Plan Item (Normalized) ----------------
class WorkshopPlanItem(db.Model):
    __tablename__ = "workshop_plan_items"
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False, index=True)
    order_index = db.Column(db.Integer, nullable=False, default=0, index=True)
    task_type = db.Column(db.String(100), nullable=False)
    duration = db.Column(db.Integer, nullable=False, default=60)  # seconds
    phase = db.Column(db.String(255), nullable=True)
    description = db.Column(db.Text, nullable=True)
    # Optional JSON blob for per-task config (presenter/speaker/vote items etc.)
    config_json = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    workshop = db.relationship("Workshop", back_populates="plan_items")



    # ---------------- ActionItem Model (NEW) ----------------
class ActionItem(db.Model):
    __tablename__ = 'action_items'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    task_id = db.Column(db.Integer, db.ForeignKey('brainstorm_tasks.id'), nullable=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    owner_participant_id = db.Column(db.Integer, db.ForeignKey('workshop_participants.id'), nullable=True, index=True)
    due_date = db.Column(db.Date, nullable=True)
    success_metric = db.Column(db.String(255), nullable=True)
    estimated_effort_hours = db.Column(db.Float, nullable=True)
    priority = db.Column(db.String(50), nullable=False, default='medium')  # low, medium, high
    status = db.Column(db.String(50), nullable=False, default='todo')  # todo, in_progress, done, blocked
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    workshop = db.relationship('Workshop', backref=db.backref('action_items', lazy='dynamic', cascade='all, delete-orphan'))
    task = db.relationship('BrainstormTask', backref=db.backref('action_items', lazy='dynamic'))
    owner_participant = db.relationship('WorkshopParticipant', backref=db.backref('owned_action_items', lazy='dynamic'))

# ---------------- Workshop Participant Model ----------------
class WorkshopParticipant(db.Model):
    __tablename__ = "workshop_participants"
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    role = db.Column(db.String(50), default="participant") # organizer, participant
    status = db.Column(db.String(50), default="invited") # invited, accepted, declined
    invitation_token = db.Column(db.String(64), unique=True, nullable=True) # Token for accept/decline link
    token_expires = db.Column(db.DateTime, nullable=True) # Expiration for the token
    joined_timestamp = db.Column(db.DateTime, nullable=True) # When they accepted

    # --- ADDED FOR VOTING ---
    dots_remaining = db.Column(db.Integer, default=5) # Example: Start with 5 dots
    # ------------------------


    # Relationships
    workshop = db.relationship("Workshop", back_populates="participants")
    user = db.relationship("User", back_populates="workshop_participations")
    submitted_ideas = db.relationship("BrainstormIdea", back_populates="participant", cascade="all, delete-orphan", lazy='dynamic') # Added backref
    votes_cast = db.relationship("IdeaVote", back_populates="participant", cascade="all, delete-orphan", lazy='dynamic') # Added backref


    # Unique constraint
    __table_args__ = (db.UniqueConstraint('workshop_id', 'user_id', name='_workshop_user_uc'),)

    # Helper function to generate and validate tokens.
    def generate_token(self):
        self.invitation_token = secrets.token_urlsafe(32)
        self.token_expires = datetime.utcnow() + timedelta(days=7) # Example: 7-day expiry
    def is_token_valid(self):
        return self.invitation_token and self.token_expires and self.token_expires > datetime.utcnow()


# ---------------- Workshop Document Link Model ----------------
class WorkshopDocument(db.Model):
    __tablename__ = "workshop_documents"
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    workshop = db.relationship("Workshop", back_populates="linked_documents")
    document = db.relationship("Document", back_populates="workshop_links")

    # Unique constraint
    __table_args__ = (db.UniqueConstraint('workshop_id', 'document_id', name='_workshop_document_uc'),)
    
    
# ---------------- BrainstormTask Model ---------------------------
class BrainstormTask(db.Model):
    __tablename__ = "brainstorm_tasks"

    # Task details
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    task_type = db.Column(db.String(100), nullable=False) # <-- ADDED THIS FIELD  
    title = db.Column(db.String(255), nullable=False)            # e.g. "Introduction"
    description = db.Column(db.Text, nullable=True) # <-- ADDED THIS FIELD  
    prompt = db.Column(db.Text, nullable=True)                   # Legacy: full JSON payload string
    payload_json = db.Column(db.Text, nullable=True)             # New: structured JSON payload (stringified)

    # Timer
    duration = db.Column(db.Integer, nullable=False)             # The task duration in seconds
    status = db.Column(db.String(50), default="pending")         # STATUS: 'pending', 'running','completed', 'skipped'
    started_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- ADDED FOR CLUSTERING ---
    # Relationship
    workshop = db.relationship("Workshop", back_populates="tasks", foreign_keys=[workshop_id]) # Explicit FK here too for clarity, matching Workshop.tasks
    ideas = db.relationship("BrainstormIdea", back_populates="task",
                            cascade="all, delete-orphan", lazy="dynamic", order_by="BrainstormIdea.timestamp")
    # Clusters created during/after clustering/voting phases
    clusters = db.relationship("IdeaCluster", back_populates="task", cascade="all, delete-orphan", lazy='dynamic')


@event.listens_for(BrainstormTask, "after_delete")
def _clear_current_task_references(mapper, connection, task):
    connection.execute(
        text("UPDATE workshops SET current_task_id = NULL WHERE current_task_id = :task_id"),
        {"task_id": task.id},
    )
    connection.execute(
        text("UPDATE workspaces SET current_task_id = NULL WHERE current_task_id = :task_id"),
        {"task_id": task.id},
    )


# ---------------- BrainstormIdea Model ---------------------------
class BrainstormIdea(db.Model):
    __tablename__ = "brainstorm_ideas"
    
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("brainstorm_tasks.id"), nullable=False)
    participant_id = db.Column(db.Integer, db.ForeignKey("workshop_participants.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    corrected_text = db.Column(db.Text, nullable=True)  # 
    source = db.Column(db.String(16), nullable=False, default="human", index=True)
    duplicate_of_id = db.Column(db.Integer, db.ForeignKey("brainstorm_ideas.id"), nullable=True)
    rationale = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    include_in_outputs = db.Column(db.Boolean, nullable=False, default=True)
    # votes = db.relationship("IdeaVote", back_populates="idea", cascade="all, delete-orphan", lazy='dynamic')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    # --- ADDED/MODIFIED FOR CLUSTERING ---
    cluster_id = db.Column(db.Integer, db.ForeignKey("idea_clusters.id"), nullable=True)
    cluster = db.relationship(
        "IdeaCluster",
        back_populates="ideas",
        foreign_keys=[cluster_id],               # <— important
        primaryjoin="BrainstormIdea.cluster_id == IdeaCluster.id",
    )
    # -----------------------------------
    
    
    # Relationships
    task = db.relationship("BrainstormTask", back_populates="ideas")
    participant = db.relationship("WorkshopParticipant", back_populates="submitted_ideas") # Use back_populates
    # votes = db.relationship("IdeaVote", back_populates="idea", cascade="all, delete-orphan", lazy='dynamic') # Added backref

    
    # Remove Idea
    # cluster_id = db.Column(db.Integer, db.ForeignKey("idea_clusters.id"), nullable=True)
    # cluster = db.relationship("IdeaCluster", back_populates="ideas")
    
    # ... (Remove IdeaCluster, IdeaVote, ActivityLog, SubmittedIdea, WorkshopTask if not used for core persistence) ...
    # Keep ChatMessage as it's part of the persistence requirement
    

# --- ADDED IdeaCluster model definition based on previous context ---
class IdeaCluster(db.Model):
    __tablename__ = "idea_clusters"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("brainstorm_tasks.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    theme_gist = db.Column(db.Text, nullable=True) # Optional theme gist or summary
    representative_idea_id = db.Column(db.Integer, db.ForeignKey("brainstorm_ideas.id"), nullable=True) # Optional link to a representative idea
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    ideas = db.relationship(
        "BrainstormIdea",
        back_populates="cluster",
        foreign_keys="BrainstormIdea.cluster_id",  # <— important
        primaryjoin="BrainstormIdea.cluster_id == IdeaCluster.id",
        lazy='dynamic',
    )
    task = db.relationship("BrainstormTask", back_populates="clusters") # Relationship back to the voting task
    votes = db.relationship("IdeaVote", back_populates="cluster", cascade="all, delete-orphan", lazy='dynamic') # Votes for this cluster
    representative_idea = db.relationship(
        "BrainstormIdea",
        foreign_keys=[representative_idea_id],     # <— important
        uselist=False,
    )



# --- ADDED IdeaVote model definition based on previous context ---
class IdeaVote(db.Model):
    __tablename__ = "idea_votes"
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.Integer, db.ForeignKey("idea_clusters.id"), nullable=False)
    participant_id = db.Column(db.Integer, db.ForeignKey("workshop_participants.id"), nullable=False)
    idea_id = db.Column(db.Integer, db.ForeignKey("brainstorm_ideas.id"), nullable=True) # Optional, if voting on specific ideas
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    dots_used = db.Column(db.Integer, default=1) # Number of dots used in this vote
    # --- MODIFIED: Unique constraint per participant per cluster ---
    __table_args__ = (db.UniqueConstraint('cluster_id', 'participant_id', name='_cluster_participant_uc'),)
    # Relationships
    # idea = db.relationship("BrainstormIdea", back_populates="votes") # Remove if voting on clusters
    cluster = db.relationship("IdeaCluster", back_populates="votes") # Link to cluster
    participant = db.relationship("WorkshopParticipant", back_populates="votes_cast") # Use back_populates


# --- Legacy compatibility aliases ---
Vote = IdeaVote
Cluster = IdeaCluster


# --- NEW: GenericVote model for vote_generic tasks ---
class GenericVote(db.Model):
    __tablename__ = 'generic_votes'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('brainstorm_tasks.id'), nullable=False, index=True)
    participant_id = db.Column(db.Integer, db.ForeignKey('workshop_participants.id'), nullable=False, index=True)
    # Item identity as emitted in vote_generic payload
    item_type = db.Column(db.String(32), nullable=False)
    item_id = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('task_id', 'participant_id', 'item_type', 'item_id', name='_generic_vote_unique'),
    )

    # Relationships
    task = db.relationship('BrainstormTask', backref=db.backref('generic_votes', lazy='dynamic', cascade='all, delete-orphan'))
    participant = db.relationship('WorkshopParticipant', backref=db.backref('generic_votes', lazy='dynamic', cascade='all, delete-orphan'))


# --- ADDED ActivityLog model definition based on previous context ---
class ActivityLog(db.Model):
    __tablename__ = "activity_logs"
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey("workshop_participants.id"), nullable=True)
    task_id = db.Column(db.Integer, db.ForeignKey("brainstorm_tasks.id"), nullable=True)
    idea_id = db.Column(db.Integer, db.ForeignKey("brainstorm_ideas.id"), nullable=True)
    vote_id = db.Column(db.Integer, db.ForeignKey("idea_votes.id"), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships (Assuming related models have back_populates='logs')
    participant = db.relationship("WorkshopParticipant") # Add back_populates="logs" in WorkshopParticipant if needed
    task        = db.relationship("BrainstormTask") # Add back_populates="logs" in BrainstormTask if needed
    idea        = db.relationship("BrainstormIdea") # Add back_populates="logs" in BrainstormIdea if needed
    vote        = db.relationship("IdeaVote") # Add back_populates="logs" in IdeaVote if needed



# ---------------- ChatMessage Model ---------------------------
class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    username = db.Column(db.String(100), nullable=False)
    message = db.Column(db.Text, nullable=False)
    role = db.Column(db.String(20), nullable=False, default='participant', index=True) # 'organizer', 'participant', 'system'
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # New: message type for styling/filtering (user, system, facilitator)
    message_type = db.Column(db.String(20), nullable=False, default='user', index=True)
    # New: chat scope to separate workshop room chat vs deeper discussion thread
    # Allowed values: 'workshop_chat' | 'discussion_chat'
    chat_scope = db.Column(db.String(20), nullable=False, default='workshop_chat', index=True)

    # Relationships
    workshop = db.relationship("Workshop", back_populates="chat_messages")
    user = db.relationship("User")


# ---------------- Idea Tagging & Favorites (NEW) ----------------
idea_tag_association = db.Table(
    'brainstorm_idea_tags',
    db.Column('idea_id', db.Integer, db.ForeignKey('brainstorm_ideas.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('idea_tags.id'), primary_key=True),
    db.UniqueConstraint('idea_id', 'tag_id', name='_idea_tag_uc')
)


class IdeaTag(db.Model):
    __tablename__ = 'idea_tags'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    ideas = db.relationship('BrainstormIdea', secondary=idea_tag_association, backref=db.backref('tags', lazy='dynamic'))


class FavoriteIdea(db.Model):
    __tablename__ = 'favorite_ideas'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    idea_id = db.Column(db.Integer, db.ForeignKey('brainstorm_ideas.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'idea_id', name='_user_idea_fav_uc'),)

class WorkshopAgenda(db.Model):
    __tablename__ = 'workshop_agenda'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False)
    activity_title = db.Column(db.Text, nullable=False)
    activity_description = db.Column(db.Text, nullable=True)
    estimated_duration = db.Column(db.Integer, nullable=True)  # minutes
    generated_source = db.Column(db.String(20), nullable=False, default='organizer')  # 'llm','organizer','edited'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    start_offset = db.Column(db.Integer, nullable=True)  # seconds from start
    end_offset = db.Column(db.Integer, nullable=True)
    time_slot = db.Column(db.String(50), nullable=True)  # original raw string e.g. '00:00:00 - 00:30:00'
    task_type = db.Column(db.String(64), nullable=True)
    origin = db.Column(db.String(16), nullable=False, default='organizer')
    duration_minutes = db.Column(db.Integer, nullable=True)

    workshop = db.relationship('Workshop', back_populates='agenda_items')

    __table_args__ = (
        db.UniqueConstraint('workshop_id', 'position', name='_ws_agenda_position_uc'),
    )


# ---------------- Transcript (Final Utterances) Model ----------------
class Transcript(db.Model):
    """Stores a finalized utterance for a single speaker during a workshop.
    One row per final chunk; partials never persisted here. Processed text can
    be auto-corrected / cleaned post STT.
    """
    __tablename__ = 'transcripts'
    transcript_id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    # Optional link to the task this utterance belongs to (used for facilitator idempotency)
    task_id = db.Column(db.Integer, db.ForeignKey('brainstorm_tasks.id'), nullable=True, index=True)
    # 'human' for participant/organizer speech; 'facilitator' for AI narration
    entry_type = db.Column(db.String(20), nullable=False, default='human', index=True)
    raw_stt_transcript = db.Column(db.Text, nullable=True)
    processed_transcript = db.Column(db.Text, nullable=True)
    language = db.Column(db.String(16), nullable=True)
    start_timestamp = db.Column(db.DateTime, nullable=True)
    end_timestamp = db.Column(db.DateTime, nullable=True)
    confidence = db.Column(db.Float, nullable=True)
    created_timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship('User')
    workshop = db.relationship('Workshop', backref=db.backref('transcripts', lazy='dynamic', cascade='all, delete-orphan'))
    task = db.relationship('BrainstormTask', backref=db.backref('transcripts', lazy='dynamic'))

    # For databases that support it, we want to enforce a uniqueness for facilitator entries
    # per (workshop_id, task_id). SQLite doesn't support partial unique indexes easily via
    # SQLAlchemy across versions, so we enforce in code and provide a helper migration script
    # to create a unique index where possible.

    def as_dict(self):
        return {
            'transcript_id': self.transcript_id,
            'workshop_id': self.workshop_id,
            'user_id': self.user_id,
            'task_id': getattr(self, 'task_id', None),
            'entry_type': getattr(self, 'entry_type', None),
            'raw_stt_transcript': self.raw_stt_transcript,
            'processed_transcript': self.processed_transcript,
            'language': self.language,
            'start_timestamp': self.start_timestamp.isoformat() if self.start_timestamp else None,
            'end_timestamp': self.end_timestamp.isoformat() if self.end_timestamp else None,
            'confidence': self.confidence,
            'created_timestamp': self.created_timestamp.isoformat() if self.created_timestamp else None,
        }


# ---------------- Dialogue (Streaming / In‑Progress Lines) Model ----------------
class Dialogue(db.Model):
    """Lightweight stream log used to render *current* speaker lines quickly.
    Can be optionally linked to a final Transcript row when finalized.
    Keeping this separate allows future features (live sentiment, etc.) without
    bloating the Transcript table.
    """
    __tablename__ = 'dialogue'
    dialogue_id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    speaker_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    transcript_id = db.Column(db.Integer, db.ForeignKey('transcripts.transcript_id'), nullable=True, index=True)
    dialogue_text = db.Column(db.Text, nullable=True)
    is_final = db.Column(db.Boolean, default=False, nullable=False)
    created_timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    speaker = db.relationship('User')
    transcript = db.relationship('Transcript', backref=db.backref('dialogue_rows', lazy='dynamic'))
    workshop = db.relationship('Workshop', backref=db.backref('dialogue_rows', lazy='dynamic', cascade='all, delete-orphan'))

    def as_dict(self):
        return {
            'dialogue_id': self.dialogue_id,
            'workshop_id': self.workshop_id,
            'speaker_id': self.speaker_id,
            'transcript_id': self.transcript_id,
            'dialogue_text': self.dialogue_text,
            'is_final': self.is_final,
            'created_timestamp': self.created_timestamp.isoformat() if self.created_timestamp else None,
        }


# ---------------- Conference Media State (Video / Audio / Screen) ----------------
class ConferenceMediaState(db.Model):
    """Stores the latest known media state (mic/cam/screen) for a participant in a workshop conference.
    Used for analytics and late joiners, and can be extended with bitrate / resolution metrics later.
    """
    __tablename__ = 'conference_media_state'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    mic_enabled = db.Column(db.Boolean, default=True, nullable=False)
    cam_enabled = db.Column(db.Boolean, default=True, nullable=False)
    screen_sharing = db.Column(db.Boolean, default=False, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    __table_args__ = (db.UniqueConstraint('workshop_id', 'user_id', name='_conf_media_ws_user_uc'),)

    user = db.relationship('User')
    workshop = db.relationship('Workshop', backref=db.backref('media_states', lazy='dynamic', cascade='all, delete-orphan'))

    def as_dict(self):
        return {
            'workshop_id': self.workshop_id,
            'user_id': self.user_id,
            'mic_enabled': self.mic_enabled,
            'cam_enabled': self.cam_enabled,
            'screen_sharing': self.screen_sharing,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# Helpful composite indexes (created after model definition when metadata creates tables)
db.Index('idx_transcripts_ws_time', Transcript.workshop_id, Transcript.created_timestamp)
db.Index('idx_transcripts_user_time', Transcript.user_id, Transcript.created_timestamp)
db.Index('idx_dialogue_ws_time', Dialogue.workshop_id, Dialogue.created_timestamp)
db.Index('idx_dialogue_speaker_time', Dialogue.speaker_id, Dialogue.created_timestamp)

    

# ---------------- LLM Usage Log (for Bedrock/Nova and others) ----------------
class LLMUsageLog(db.Model):
    """Lightweight audit table for LLM usage.

    Captures per-call metadata and optional feedback for outputs generated by
    the application (e.g., transcript polishing). Designed to be append-only.
    """
    __tablename__ = 'llm_usage_logs'
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=True, index=True)
    transcript_id = db.Column(db.Integer, db.ForeignKey('transcripts.transcript_id'), nullable=True, index=True)

    service_used = db.Column(db.String(64), nullable=False, default='bedrock')  # e.g., 'bedrock'
    model_used = db.Column(db.String(128), nullable=True)  # e.g., 'amazon.nova-lite-v1:0'

    prompt_input_size = db.Column(db.Integer, nullable=True)  # total chars sent across turns
    response_size = db.Column(db.Integer, nullable=True)      # total chars received across turns
    token_usage = db.Column(db.String(64), nullable=True)     # provider-specific usage info (text)
    latency_ms = db.Column(db.Integer, nullable=True)

    # Optional user feedback on the generated output
    feedback_vote = db.Column(db.Integer, nullable=True)      # +1 / 0 / -1
    feedback_comment = db.Column(db.Text, nullable=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.id'), nullable=True)
    created_timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Relationships
    workshop = db.relationship('Workshop')
    transcript = db.relationship('Transcript')

    def as_dict(self):
        return {
            'id': self.id,
            'workshop_id': self.workshop_id,
            'transcript_id': self.transcript_id,
            'service_used': self.service_used,
            'model_used': self.model_used,
            'prompt_input_size': self.prompt_input_size,
            'response_size': self.response_size,
            'token_usage': self.token_usage,
            'latency_ms': self.latency_ms,
            'feedback_vote': self.feedback_vote,
            'feedback_comment': self.feedback_comment,
            'created_timestamp': self.created_timestamp.isoformat() if self.created_timestamp else None,
        }

class CapturedDecision(db.Model):
    __tablename__ = "captured_decisions"

    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    cluster_id = db.Column(db.Integer, db.ForeignKey("idea_clusters.id"), nullable=True)

    topic = db.Column(db.String(255), nullable=False)
    decision = db.Column(db.Text, nullable=False)
    rationale = db.Column(db.Text, nullable=True)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True)

    status = db.Column(db.String(32), nullable=False, default="draft")
    confirmed_at = db.Column(db.DateTime, nullable=True)
    confirmed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    workshop = db.relationship("Workshop", backref=db.backref("captured_decisions", lazy="dynamic"))
    cluster = db.relationship("IdeaCluster", backref=db.backref("captured_decisions", lazy="dynamic"))
    confirmed_by = db.relationship("User", backref=db.backref("confirmed_decisions", lazy="dynamic"), foreign_keys=[confirmed_by_user_id])


class DiscussionNote(db.Model):
    __tablename__ = "discussion_notes"

    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    speaker_user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True)

    ts = db.Column(db.DateTime, default=datetime.utcnow)  # timestamp of the note
    point = db.Column(db.Text, nullable=False)
    origin = db.Column(db.String(32), nullable=False, default="chat")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    workshop = db.relationship("Workshop", backref=db.backref("discussion_notes", lazy="dynamic"))
    speaker = db.relationship("User", backref=db.backref("discussion_notes", lazy="dynamic"), foreign_keys=[speaker_user_id])


class DiscussionSettings(db.Model):
    __tablename__ = "discussion_settings"

    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False, unique=True)

    mediator_interval_secs = db.Column(db.Integer, nullable=False, default=300)
    scribe_interval_secs = db.Column(db.Integer, nullable=False, default=240)
    last_mediator_run_at = db.Column(db.DateTime, nullable=True)
    last_scribe_run_at = db.Column(db.DateTime, nullable=True)
    auto_seed_forum = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workshop = db.relationship("Workshop", backref=db.backref("discussion_settings", uselist=False, cascade="all, delete-orphan"))


class DiscussionRun(db.Model):
    __tablename__ = "discussion_runs"

    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False, index=True)
    mode = db.Column(db.String(32), nullable=False)
    llm_model = db.Column(db.String(128), nullable=True)
    latency_ms = db.Column(db.Integer, nullable=True)
    input_checksum = db.Column(db.String(64), nullable=True)
    response_json = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True)

    workshop = db.relationship("Workshop", backref=db.backref("discussion_runs", lazy="dynamic"))
    created_by = db.relationship("User", backref=db.backref("discussion_runs", lazy="dynamic"), foreign_keys=[created_by_id])
