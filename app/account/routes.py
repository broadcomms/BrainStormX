# app/account/routes.py

import os
from typing import Any
from flask import Blueprint, render_template, flash, redirect, url_for, current_app, request
from flask_login import login_required, current_user, logout_user
# Import necessary models
from app.models import User, Workspace, WorkspaceMember, Invitation, Workshop, Document, WorkshopParticipant, FavoriteIdea, ChatMessage
from sqlalchemy.orm import joinedload
# Import database instance
from app import db 
from sqlalchemy import or_, desc
from datetime import datetime
from app.config import Config
from werkzeug.utils import secure_filename
from flask import Response
import json as _json
import secrets
from passlib.hash import bcrypt

account_bp = Blueprint("account_bp", __name__, template_folder="templates")

# --- Define the default path as a constant ---
DEFAULT_PROFILE_PIC = "images/default-profile.png"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

def path_exists_in_static(relative_path: str) -> bool:
    if not relative_path:
        return False
    static_folder = current_app.static_folder or ""
    full_path = os.path.join(static_folder, relative_path)
    return os.path.isfile(full_path)

@account_bp.route("/")
@login_required
def account():
    """Account page"""
    # If user isn't 'user', block them
    if current_user.role not in ["user", "manager", "admin"]:
        flash("Access denied.", "danger")
        return redirect(url_for("main_bp.index"))

    # --- Normalize legacy/missing profile pic only when clearly invalid ---
    # Keep valid media URLs (/media/...), external URLs, and existing /static paths intact.
    p = (current_user.profile_pic_url or '').strip()
    if not p:
        # Leave empty; template helper will fallback to default image.
        pass
    elif p.startswith('instance/'):
        # Legacy bad path; swap to default to avoid broken image.
        current_user.profile_pic_url = DEFAULT_PROFILE_PIC
    elif p.startswith('/media/') or p.startswith('/static/') or p.startswith('http://') or p.startswith('https://'):
        # Valid URL forms for media/static/external; do nothing.
        pass
    else:
        # Treat as relative static asset; if missing on disk, use default.
        if not path_exists_in_static(p):
            current_user.profile_pic_url = DEFAULT_PROFILE_PIC

    # --- Fetch User's Active Workspaces ---
    user_memberships = WorkspaceMember.query.filter_by(user_id=current_user.user_id, status='active').all()
    my_workspace_ids = [m.workspace_id for m in user_memberships]
    my_workspaces = []
    if my_workspace_ids:
        my_workspaces = Workspace.query.filter(Workspace.workspace_id.in_(my_workspace_ids)).order_by(Workspace.name).all()

    # --- Invitations ---
    pending_invitations = Invitation.query.filter_by(email=current_user.email, status='pending').order_by(desc(Invitation.sent_timestamp)).all()

    # --- Workshops (recent, upcoming, past) ---
    now = datetime.utcnow()
    workshops = []
    upcoming_workshops, past_workshops = [], []
    if my_workspace_ids:
        workshops = Workshop.query.filter(Workshop.workspace_id.in_(my_workspace_ids)).order_by(desc(Workshop.date_time)).limit(10).all()
        upcoming_workshops = Workshop.query.filter(Workshop.workspace_id.in_(my_workspace_ids), Workshop.date_time >= now).order_by(Workshop.date_time.asc()).limit(5).all()
        past_workshops = Workshop.query.filter(Workshop.workspace_id.in_(my_workspace_ids), Workshop.date_time < now).order_by(desc(Workshop.date_time)).limit(5).all()

    # --- Documents ---
    recent_documents = []
    total_documents = 0
    if my_workspace_ids:
        recent_documents = Document.query.filter(Document.workspace_id.in_(my_workspace_ids)).order_by(desc(Document.uploaded_at)).limit(8).all()
        total_documents = Document.query.filter(Document.workspace_id.in_(my_workspace_ids)).count()

    # --- Members / Collaborators Panel Data ---
    recent_collaborators = []  # Active members ordered by most recent join
    pending_requests = []      # Pending (requested) memberships needing approval
    total_members = 0
    if my_workspace_ids:
        # All active member records (excluding self) for recency ordering
        active_member_rows = (WorkspaceMember.query
                               .filter(WorkspaceMember.workspace_id.in_(my_workspace_ids),
                                       WorkspaceMember.status == 'active',
                                       WorkspaceMember.user_id != current_user.user_id)
                               .order_by(WorkspaceMember.joined_timestamp.desc())
                               .limit(20)
                               .all())
        recent_collaborators = [r for r in active_member_rows if r.user]  # keep full membership objects
        # Distinct active member user count
        total_members = (db.session.query(WorkspaceMember.user_id)
                          .filter(WorkspaceMember.workspace_id.in_(my_workspace_ids), WorkspaceMember.status == 'active')
                          .distinct().count())

        # Workspaces where current user can approve (owner/admin/manager)
        admin_workspace_ids = []
        for m in user_memberships:
            if m.role in ['admin','manager']:
                admin_workspace_ids.append(m.workspace_id)
        # Include owned workspaces (owner may not have explicit membership role set differently)
        owned_ids = [ws.workspace_id for ws in my_workspaces if ws.owner_id == current_user.user_id]
        admin_workspace_ids = list({*admin_workspace_ids, *owned_ids})
        if admin_workspace_ids:
            pending_requests = (WorkspaceMember.query
                                 .filter(WorkspaceMember.workspace_id.in_(admin_workspace_ids), WorkspaceMember.status == 'requested')
                                 .order_by(WorkspaceMember.joined_timestamp.desc())
                                 .limit(25)
                                 .all())

    # --- KPIs ---
    total_workspaces = len(my_workspaces)
    total_workshops = 0
    if my_workspace_ids:
        total_workshops = Workshop.query.filter(Workshop.workspace_id.in_(my_workspace_ids)).count()
    pending_invitation_count = len(pending_invitations)

    # --- Tasks placeholder (future) ---
    tasks: list[dict[str, Any]] = []
    total_tasks = 0

    # Unified display name via model property (first+last -> username -> email local part -> User{id})
    display_name = current_user.display_name
    APP_NAME = current_app.config.get("APP_NAME", "BrainStormX")

    return render_template(
        "account_dashboard.html",
        user=current_user,
        app_name=APP_NAME,
        my_workspaces=my_workspaces,
        pending_invitations=pending_invitations,
        workshops=workshops,
        upcoming_workshops=upcoming_workshops,
        past_workshops=past_workshops,
    tasks=tasks,
    recent_collaborators=recent_collaborators,
    pending_requests=pending_requests,
        recent_documents=recent_documents,
        total_workspaces=total_workspaces,
        total_workshops=total_workshops,
        total_documents=total_documents,
        total_members=total_members,
        total_tasks=total_tasks,
        pending_invitation_count=pending_invitation_count,
        display_name=display_name,
        default_profile_pic=DEFAULT_PROFILE_PIC
    )


##############################################################################
# Edit Account (Email, Username, and Profile Data)
##############################################################################
@account_bp.route("/edit_account", methods=["GET", "POST"])
@login_required
def edit_account():
    """
    Allows the user to edit their personal information,
    including first/last name, job title, phone, etc.
    Only admin/manager can edit other users if needed (by passing user_id?).
    """
    user_id = request.args.get("user_id", type=int, default=current_user.user_id)

    # Only admin/manager can edit someone else's data
    if user_id != current_user.user_id:
        if current_user.role not in ["admin", "manager"]:
            flash("You do not have permission to edit another user's account.", "danger")
            return redirect(url_for("account_bp.account"))

    user_to_edit = User.query.get_or_404(user_id)

    if request.method == "POST":
        new_username = request.form.get("username", "").strip() or None
        new_email = request.form.get("email", "").strip().lower()
        new_first_name = request.form.get("first_name", "").strip() or None
        new_last_name = request.form.get("last_name", "").strip() or None
        new_job_title = request.form.get("job_title", "").strip() or None
        new_organization = request.form.get("organization", "").strip() or None
        new_phone_number = request.form.get("phone_number", "").strip() or None
        new_is_public = bool(request.form.get("is_public_profile"))

        if not new_email:
            flash("Email is required.", "danger")
            return redirect(url_for("account_bp.edit_account", user_id=user_to_edit.user_id))

        # Unique email/username check
        conflict = User.query.filter(
            (User.user_id != user_to_edit.user_id) &
            ((User.email == new_email) | (User.username == new_username))
        ).first()
        if conflict:
            flash("Email or username already in use.", "danger")
            return redirect(url_for("account_bp.edit_account", user_id=user_to_edit.user_id))

        user_to_edit.username = new_username
        user_to_edit.email = new_email
        user_to_edit.first_name = new_first_name
        user_to_edit.last_name = new_last_name
        user_to_edit.job_title = new_job_title
        user_to_edit.organization = new_organization
        user_to_edit.phone_number = new_phone_number
        user_to_edit.is_public_profile = new_is_public

        try:
            db.session.commit()
            flash("Account information updated successfully!", "success")
        except Exception:
            db.session.rollback()
            flash("Unable to update profile right now.", "danger")
        return redirect(url_for("account_bp.edit_account", user_id=user_to_edit.user_id))
    
    return render_template("account_edit.html", user=user_to_edit)



##############################################################################
# Update Profile Photo
##############################################################################
def allowed_file(filename):
    return bool(filename) and ('.' in filename) and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@account_bp.route("/update_photo", methods=["GET", "POST"])
@login_required
def update_photo():
    """
    Updates the current user's profile photo.
    The file is saved under static/uploads/profile_pics.
    The relative URL is stored in the user's profile_pic_url column.
    """
    # Import Bedrock configured checker and telemetry reader lazily to avoid hard deps
    from app.utils.llm_bedrock import is_bedrock_configured
    from app.utils.telemetry import read_recent_events

    bedrock_flag = is_bedrock_configured()
    recent_events = []
    try:
        if getattr(current_user, "role", None) == "admin":
            recent_events = read_recent_events(25)
    except Exception:
        recent_events = []

    if request.method == "POST":
        file = request.files.get("profile_pic")
        if not file or not file.filename:
            flash("No file provided.", "danger")
            return redirect(url_for("account_bp.update_photo"))
        if not allowed_file(file.filename):
            flash("Invalid file type. Only PNG/JPG/JPEG/GIF are allowed.", "danger")
            return redirect(url_for("account_bp.update_photo"))

        # Save into instance/uploads/photos via service util
        from app.config import Config as _Cfg
        os.makedirs(_Cfg.MEDIA_PHOTOS_DIR, exist_ok=True)
        from datetime import datetime as _dt
        ts = _dt.utcnow().strftime("%Y%m%d%H%M%S")
        fname = secure_filename(f"u{current_user.user_id}_{ts}_{file.filename}")
        with open(os.path.join(_Cfg.MEDIA_PHOTOS_DIR, fname), "wb") as fh:
            fh.write(file.read())
        current_user.profile_pic_url = f"{_Cfg.MEDIA_PHOTOS_URL_PREFIX}/{fname}"
        db.session.commit()

        flash("Profile photo updated successfully!", "success")
        return redirect(url_for("account_bp.account"))
    
    return render_template("account_photo.html", bedrock_configured=bedrock_flag, recent_events=recent_events)

##############################################################################
# Public Account Profile (visible to authenticated users)
##############################################################################
@account_bp.route("/profile/<int:user_id>")
@login_required
def public_profile(user_id):
    profile_user = User.query.get_or_404(user_id)

    memberships = (WorkspaceMember.query
                   .filter_by(user_id=user_id, status='active')
                   .order_by(WorkspaceMember.joined_timestamp.desc())
                   .all())
    active_workspaces = [m.workspace for m in memberships if m.workspace]

    recent_documents = (Document.query
                        .filter_by(uploaded_by_id=user_id)
                        .order_by(Document.uploaded_at.desc())
                        .limit(8)
                        .all())

    recent_created_workshops = (Workshop.query
                                .filter_by(created_by_id=user_id)
                                .order_by(Workshop.created_at.desc())
                                .limit(6)
                                .all())

    participation_q = (WorkshopParticipant.query
                       .filter_by(user_id=user_id, status='accepted')
                       .order_by(WorkshopParticipant.joined_timestamp.desc()))
    recent_participations = participation_q.limit(6).all()

    stats = {
        'workspaces': len(active_workspaces),
        'documents': Document.query.filter_by(uploaded_by_id=user_id).count(),
        'workshops_created': Workshop.query.filter_by(created_by_id=user_id).count(),
        'workshops_participated': participation_q.count()
    }

    # --- Invite context (show actions similar to account_people) ---
    can_invite = (
        current_user.is_authenticated
        and current_user.user_id != profile_user.user_id
        and bool(getattr(profile_user, 'is_public_profile', True))
    )

    # Workspaces where the viewer can invite (admin/manager)
    invite_workspaces = []
    if can_invite:
        my_admin_memberships = (
            WorkspaceMember.query
            .filter(
                WorkspaceMember.user_id == current_user.user_id,
                WorkspaceMember.status == 'active',
                WorkspaceMember.role.in_(['admin', 'manager'])
            )
            .all()
        )
        invite_workspaces = [m.workspace for m in my_admin_memberships if m.workspace]

    # Workshops organized by the viewer (organizer can add participants)
    organizer_workshops = []
    if can_invite:
        organizer_workshops = (
            Workshop.query
            .filter(Workshop.created_by_id == current_user.user_id)
            .order_by(Workshop.date_time.desc().nullslast())
            .all()
        )

    return render_template(
        "account_profile.html",
        profile_user=profile_user,
        memberships=memberships,
        active_workspaces=active_workspaces,
        recent_documents=recent_documents,
        recent_created_workshops=recent_created_workshops,
        recent_participations=recent_participations,
        stats=stats,
        can_invite=can_invite,
        invite_workspaces=invite_workspaces,
        organizer_workshops=organizer_workshops,
    )
    
##############################################################################
# List of public profiles of other users/people (visible to authenticated users)
##############################################################################
@account_bp.route("/people")
@login_required
def account_people():
    # Gather all other users (could add pagination later)
    others = User.query.filter(
        (User.user_id != current_user.user_id) & (User.is_public_profile == True)
    ).order_by(User.created_timestamp.desc()).all()

    # Active workspaces for current user (for invite modal select)
    my_memberships = WorkspaceMember.query.filter_by(user_id=current_user.user_id, status='active').all()
    my_workspace_ids = [m.workspace_id for m in my_memberships]
    my_workspaces = []
    if my_workspace_ids:
        my_workspaces = Workspace.query.filter(Workspace.workspace_id.in_(my_workspace_ids)).order_by(Workspace.name.asc()).all()

    # Build lightweight stats per person (simple counts; optimize with aggregates if needed later)
    people = []
    for u in others:
        people.append({
            'user': u,
            'workspaces': WorkspaceMember.query.filter_by(user_id=u.user_id, status='active').count(),
            'documents': Document.query.filter_by(uploaded_by_id=u.user_id).count(),
            'workshops_created': Workshop.query.filter_by(created_by_id=u.user_id).count(),
            'joined': u.created_timestamp
        })

    return render_template(
        "account_people.html",
        people=people,
        my_workspaces=my_workspaces,
        default_profile_pic=DEFAULT_PROFILE_PIC,
    )

##############################################################################
# Export My Data (JSON)
##############################################################################
@account_bp.route("/export_my_data", methods=["GET"])
@login_required
def export_my_data():
    u = current_user
    # Build a compact export of user's data and associations
    memberships = WorkspaceMember.query.filter_by(user_id=u.user_id).all()
    my_workspaces = [m.workspace for m in memberships if m.workspace]
    uploaded_docs = Document.query.filter_by(uploaded_by_id=u.user_id).all()
    created_workshops = Workshop.query.filter_by(created_by_id=u.user_id).all()
    participations = WorkshopParticipant.query.filter_by(user_id=u.user_id).all()

    # Optional reclaim support: include a reclaim token to restore the account later
    include_reclaim = request.args.get("include_reclaim", "0") == "1"
    reclaim_token = None
    if include_reclaim:
        # Reuse verification_token field to store a reclaim token
        reclaim_token = secrets.token_urlsafe(32)
        u.verification_token = reclaim_token
        db.session.commit()

    export = {
        "user": {
            "user_id": u.user_id,
            "email": u.email,
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "job_title": u.job_title,
            "organization": u.organization,
            "phone_number": u.phone_number,
            "public_profile": bool(getattr(u, 'is_public_profile', False)),
            "created": u.created_timestamp.isoformat() if u.created_timestamp else None,
        },
        "memberships": [
            {
                "workspace_id": m.workspace_id,
                "workspace_name": m.workspace.name if m.workspace else None,
                "role": m.role,
                "status": m.status,
                "joined": m.joined_timestamp.isoformat() if m.joined_timestamp else None,
            }
            for m in memberships
        ],
        "uploaded_documents": [
            {
                "id": d.id,
                "workspace_id": d.workspace_id,
                "title": d.title,
                "file_name": d.file_name,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
                "size": d.file_size,
                "description": d.description,
            }
            for d in uploaded_docs
        ],
        "workshops_created": [
            {
                "id": w.id,
                "workspace_id": w.workspace_id,
                "title": w.title,
                "date_time": w.date_time.isoformat() if w.date_time else None,
                "status": w.status,
            }
            for w in created_workshops
    ],
        "workshop_participations": [
            {
                "workshop_id": p.workshop_id,
                "status": p.status,
                "role": p.role,
                "joined": p.joined_timestamp.isoformat() if p.joined_timestamp else None,
            }
            for p in participations
        ],
        "reclaim": {
            "token": reclaim_token,
            "how_to": "Keep this file safe. To reactivate later, visit /auth/reclaim/<token> and set a new password.",
            "reclaim_url": url_for("auth_bp.reclaim_account", token=reclaim_token, _external=True) if reclaim_token else None,
        } if include_reclaim else None,
    }

    data = _json.dumps(export, indent=2)
    filename = f"brainstormx_account_export_user_{u.user_id}.json"
    return Response(
        data,
        mimetype="application/json",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        },
    )


##############################################################################
# Delete Account (Graceful Exit)
##############################################################################
@account_bp.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    """Anonymize the user and remove revocable associations.

    Immutable records (completed workshops, reports, chat logs) are preserved
    with anonymized user identity to maintain system integrity.
    """
    confirm_email = request.form.get("confirm_email", "").strip().lower()
    if confirm_email != (current_user.email or "").lower():
        flash("Email did not match. Deletion cancelled.", "warning")
        return redirect(url_for("account_bp.edit_account", user_id=current_user.user_id))

    u = User.query.get_or_404(current_user.user_id)

    # Block if the user owns active workspaces
    owned_workspaces = Workspace.query.filter_by(owner_id=u.user_id).all()
    if owned_workspaces:
        titles = ", ".join([w.name for w in owned_workspaces])
        flash(
            f"Please transfer or delete your owned workspaces before deleting your account: {titles}",
            "danger",
        )
        return redirect(url_for("account_bp.account"))

    try:
        # 1) Remove workspace memberships and pending invites
        WorkspaceMember.query.filter_by(user_id=u.user_id).delete(synchronize_session=False)
        Invitation.query.filter_by(email=u.email).delete(synchronize_session=False)
        Invitation.query.filter_by(inviter_id=u.user_id).delete(synchronize_session=False)

        # 2) Delete user favorites
        FavoriteIdea.query.filter_by(user_id=u.user_id).delete(synchronize_session=False)

        # 3) For non-completed workshops where user is a participant, remove their participation
        active_participations = (
            WorkshopParticipant.query.join(Workshop, WorkshopParticipant.workshop_id == Workshop.id)
            .filter(WorkshopParticipant.user_id == u.user_id, Workshop.status != "completed")
            .all()
        )
        for p in active_participations:
            # Deleting participation cascades ideas/votes tied via participant where configured
            db.session.delete(p)

        # 4) Reassign workshops created by the user to the workspace owner
        created = Workshop.query.filter_by(created_by_id=u.user_id).all()
        for w in created:
            # If workspace exists, reassign to owner; else leave as-is
            if w.workspace and w.workspace.owner_id:
                w.created_by_id = w.workspace.owner_id

        # 5) Reassign uploaded documents to the corresponding workspace owner (keeps docs inside org)
        docs = Document.query.filter_by(uploaded_by_id=u.user_id).all()
        for d in docs:
            if d.workspace and d.workspace.owner_id:
                d.uploaded_by_id = d.workspace.owner_id

        # 6) Anonymize chat messages to keep workshop history readable
        ChatMessage.query.filter_by(user_id=u.user_id).update({
            ChatMessage.username: "Former participant",
            ChatMessage.user_id: u.user_id  # keep FK for integrity
        }, synchronize_session=False)

        # 7) Anonymize user account (retain record for FK integrity)
        tombstone_suffix = str(u.user_id)
        u.username = f"deleted-user-{tombstone_suffix}"
        u.first_name = None
        u.last_name = None
        u.job_title = None
        u.organization = None
        u.phone_number = None
        u.profile_pic_url = DEFAULT_PROFILE_PIC
        u.is_public_profile = False
        # Preserve uniqueness of email, break contactability
        u.email = f"deleted+{tombstone_suffix}@example.invalid"
        # Randomize password to prevent login and clear tokens
        u.password = bcrypt.hash(secrets.token_hex(32))
        u.verification_token = None
        u.reset_token = None
        u.reset_token_expires = None

        db.session.commit()

        # End session
        logout_user()
        flash("Your account has been deleted and personal data removed/anonymized.", "success")
        return redirect(url_for("main_bp.index"))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting account for user {u.user_id}: {e}")
        flash("We couldn't delete your account right now.", "danger")
        return redirect(url_for("account_bp.edit_account", user_id=u.user_id))