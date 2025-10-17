# app/workspace/routes.py

import os
from flask import Blueprint, request, redirect, url_for, flash, render_template, jsonify, current_app
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Workspace, WorkspaceMember, User, Invitation, Document, Workshop
from datetime import datetime
from sqlalchemy.orm import joinedload, selectinload

APP_NAME = os.getenv("APP_NAME", "BrainStormX")
workspace_bp = Blueprint("workspace_bp", __name__, template_folder="templates")


# --- Helper Function for Permission Check ---
def check_admin_permission(workspace_id, user_id):
    """Checks if the user is an admin or manager of the workspace."""
    workspace = Workspace.query.get_or_404(workspace_id)
    # Owner always has permission
    if workspace.owner_id == user_id:
        return True
    # Check for admin/manager role in WorkspaceMember
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=user_id, status='active'
    ).first()
    return bool(membership and membership.role in ['admin', 'manager'])









###################################
# 1. List Workspaces
###################################
@workspace_bp.route("/list")
@login_required
def list_workspaces():
    """
    Shows workspaces the user belongs to, and optionally public workspaces they can join.
    """
    # Workspaces the current user is in:
    my_workspace_ids = [m.workspace_id for m in current_user.workspace_memberships]
    my_workspaces = Workspace.query.filter(
        Workspace.workspace_id.in_(my_workspace_ids)
    ).all()

    # Optionally list all public workspaces that the user is not in:
    public_workspaces = (
        Workspace.query.filter_by(is_private=False)
        .filter(~Workspace.workspace_id.in_(my_workspace_ids))
        .all()
    )

    return render_template(
        "workspace_list.html",
        my_workspaces=my_workspaces,
        public_workspaces=public_workspaces,
    )

###################################
# 2. Create Workspace
###################################
@workspace_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_workspace():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        # --- ADDED: Get description ---
        description = request.form.get("description", "").strip()
        is_private = request.form.get("is_private") == "on"

        if not name:
            flash("Workspace name is required.", "danger")
            # --- Return render_template instead of redirect to preserve form data (optional but good UX) ---
            return render_template("workspace_create.html", name=name, description=description, is_private=is_private)

        # Check if name already in use
        existing = Workspace.query.filter_by(name=name).first()
        if existing:
            flash(
                f"Workspace '{name}' already exists. Please choose a different name.",
                "danger",
            )
            # --- Return render_template instead of redirect ---
            return render_template("workspace_create.html", name=name, description=description, is_private=is_private)


        # Create the workspace object (assign attributes to satisfy type checkers)
        new_workspace = Workspace()
        new_workspace.name = name
        new_workspace.owner_id = current_user.user_id
        new_workspace.is_private = is_private
        new_workspace.description = description
        db.session.add(new_workspace)

        # --- FIX: Flush the session to get the workspace_id ---
        try:
            db.session.flush() # This assigns the ID to new_workspace

            # Now new_workspace.workspace_id has a value
            owner_membership = WorkspaceMember()
            owner_membership.workspace_id = new_workspace.workspace_id  # Now this works!
            owner_membership.user_id = current_user.user_id
            owner_membership.role = "admin"
            owner_membership.status = "active"
            db.session.add(owner_membership)

            # Commit both the workspace and the membership together
            db.session.commit()
            flash("Workspace created successfully!", "success")
            return redirect(
                url_for(
                    "workspace_bp.view_workspace", workspace_id=new_workspace.workspace_id
                )
            )
        except Exception as e:
            db.session.rollback() # Rollback the transaction on error
            current_app.logger.error(f"Error creating workspace: {e}")
            flash("An error occurred while creating the workspace.", "danger")
            # It's good practice to pass the attempted values back to the form
            return render_template("workspace_create.html", name=name, description=description, is_private=is_private)

    # GET request
    return render_template("workspace_create.html")



###################################
# 3. Edit Workspace
###################################
@workspace_bp.route("/edit/<int:workspace_id>", methods=["GET", "POST"])
@login_required
def edit_workspace(workspace_id):
    """
    Allows the workspace owner or admin to edit workspace details.
    """
    workspace = Workspace.query.get_or_404(workspace_id)

    # --- Permission Check: Only owner or admin can edit ---
    is_owner = workspace.owner_id == current_user.user_id
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id, status='active'
    ).first()
    is_admin = membership and membership.role == 'admin'

    if not (is_owner or is_admin):
        flash("You do not have permission to edit this workspace.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        new_description = request.form.get("description", "").strip()
        new_is_private = request.form.get("is_private") == "on"

        if not new_name:
            flash("Workspace name cannot be empty.", "danger")
            # Re-render form with error and existing data
            return render_template("workspace_edit.html", workspace=workspace)

        # Check if new name conflicts with another workspace (excluding itself)
        existing = Workspace.query.filter(
            Workspace.name == new_name,
            Workspace.workspace_id != workspace_id
        ).first()
        if existing:
            flash(f"Workspace name '{new_name}' is already taken.", "danger")
            # Re-render form with error and existing data (pass back attempted values)
            workspace.name = new_name # Temporarily set for template rendering
            workspace.description = new_description
            workspace.is_private = new_is_private
            return render_template("workspace_edit.html", workspace=workspace)

        # Update workspace details
        workspace.name = new_name
        workspace.description = new_description
        workspace.is_private = new_is_private
        workspace.updated_timestamp = datetime.utcnow() # Explicitly update timestamp

        try:
            db.session.commit()
            flash("Workspace details updated successfully!", "success")
            return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating workspace {workspace_id}: {e}")
            flash("An error occurred while updating the workspace.", "danger")
            # Re-render form with error and existing data
            return render_template("workspace_edit.html", workspace=workspace)

    # --- GET Request: Render the edit form with current data ---
    return render_template("workspace_edit.html", workspace=workspace)


###################################
# 3b. Delete Workspace (NEW)
###################################
@workspace_bp.route("/delete/<int:workspace_id>", methods=["POST"])
@login_required
def delete_workspace(workspace_id):
    """Permanently deletes a workspace and ALL associated data."""
    workspace = Workspace.query.get_or_404(workspace_id)

    # Only owner can delete (tight restriction; adjust if admins allowed)
    if workspace.owner_id != current_user.user_id:
        flash("Only the workspace owner can delete this workspace.", "danger")
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))

    confirm_name = request.form.get('confirm_name', '').strip()
    if confirm_name != workspace.name:
        flash("Workspace name did not match. Deletion cancelled.", "warning")
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))

    try:
        # Gather file paths for documents to remove from disk after commit
        doc_files = []
        # documents relationship is dynamic; call .all() before iterating
        for doc in workspace.documents.all():
            if doc.file_path:
                doc_files.append(doc.file_path)

        db.session.delete(workspace)  # Cascades: members, documents, workshops, tasks, ideas, clusters, votes, participants, links, chat
        db.session.commit()

        # Attempt physical file deletion (best-effort; ignore failures)
        uploads_root = current_app.config.get('UPLOAD_FOLDER', 'uploads')
        import os
        for rel_path in doc_files:
            try:
                abs_path = os.path.join(uploads_root, rel_path)
                if os.path.isfile(abs_path):
                    os.remove(abs_path)
            except Exception as fe:
                current_app.logger.warning(f"File removal failed for {rel_path}: {fe}")

        flash("Workspace and all associated data deleted.", "success")
        return redirect(url_for('workspace_bp.list_workspaces'))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting workspace {workspace_id}: {e}")
        flash("An error occurred while deleting the workspace.", "danger")
        return redirect(url_for('workspace_bp.view_workspace', workspace_id=workspace_id))



###################################
# 4. View Workspace
###################################
@workspace_bp.route("/<int:workspace_id>")
@login_required
def view_workspace(workspace_id):
    """
    Displays workspace details, including members, documents, and upcoming/past workshops
    for the workspace. If private, ensure user is a member.
    """
    workspace = (
        Workspace.query.options(
            # fetch members and each member’s user in one round‑trip
            selectinload(Workspace.members).selectinload(WorkspaceMember.user)  # type: ignore[arg-type]
        )
        .get_or_404(workspace_id)
    )
    my_membership = None # Initialize


    # If private, must be a member or owner
    if workspace.is_private:
        my_membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace_id, user_id=current_user.user_id, status='active' # Ensure member is active
        ).first()
        # Also allow owner even if they somehow aren't a member (shouldn't happen with current logic)
        is_owner = workspace.owner_id == current_user.user_id
        if not my_membership and not is_owner:
            flash("This workspace is private. Access denied.", "danger")
            return redirect(url_for("workspace_bp.list_workspaces"))
    else:
         # For public workspaces, still useful to know the user's membership status/role
         my_membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace_id, user_id=current_user.user_id, status='active'
        ).first()

    # --- Prepare Member Data for Template ---
    all_members = workspace.members  # Get all members (already eager loaded)
    active_members = [m for m in all_members if m.status == 'active']
    pending_members = [m for m in all_members if m.status != 'active'] # Includes 'invited', 'requested', etc.
    active_member_count = len(active_members)

    # --- Fetch Workspace Documents ---
    workspace_documents = Document.query.options(
            joinedload(Document.uploader)  # type: ignore[arg-type]  # Eager load uploader details
        ).filter_by(
            workspace_id=workspace_id
        ).order_by(
            Document.uploaded_at.desc() # Show newest first
        ).all()

    # --- Fetch Workspace Workshops ---
    # Eager load creator to avoid N+1 in template
    workspace_workshops = Workshop.query.options(
            joinedload(Workshop.creator)  # type: ignore[arg-type]
        ).filter_by(
            workspace_id=workspace_id
        ).order_by(
            Workshop.date_time.asc() # Show upcoming first
        ).all()
    
    # --- Determine Edit Permission ---
    can_edit = (workspace.owner_id == current_user.user_id) or \
               (my_membership and my_membership.role == 'admin')

    can_manage_members = (workspace.owner_id == current_user.user_id) or \
                         (my_membership and my_membership.role in ['admin', 'manager'])
                         
    # --- Determine Workshop Creation Permission ---
    # Any active member can create a workshop (adjust if needed)
    can_create_workshop = my_membership is not None and my_membership.status == 'active'

    # --- Render Template with Prepared Data ---
    return render_template( 
        "workspace_details.html",
        workspace=workspace,
        documents=workspace_documents,
        my_membership=my_membership,
        can_edit=can_edit,
        can_manage_members=can_manage_members, # Pass this flag
        # Pass the prepared member lists and count
        active_members=active_members,
        pending_members=pending_members,
        active_member_count=active_member_count,
        # --- ADDED: Pass workshops and permission ---
        workshops=workspace_workshops,
        can_create_workshop=can_create_workshop
    )

###################################
# 5. Invite User to Workspace
###################################
@workspace_bp.route("/invite_member", methods=["POST"])
@login_required
def invite_member():
    """
    Admin/manager can invite a user by email.
    If the user exists, create a WorkspaceMember with status 'invited'
    and record an Invitation so they can choose to accept or decline.
    If the user does not exist, create an Invitation record and send a registration link.
    """
    import secrets

    workspace_id = request.form.get("workspace_id", type=int)
    email = request.form.get("email", "").strip().lower()
    custom_message = request.form.get("custom_message", "").strip()
    workspace = Workspace.query.get_or_404(workspace_id)

    # Check permission
    my_membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id
    ).first()
    if not my_membership or my_membership.role not in ["admin", "manager"]:
        flash("You do not have permission to invite members.", "danger")
        return redirect(
            url_for("workspace_bp.view_workspace", workspace_id=workspace_id)
        )

    from app.auth.routes import send_email

    invited_user = User.query.filter_by(email=email).first()
    
    # generate one token for either branch
    invitation_token = secrets.token_urlsafe(32)

    # For existing users:
    if invited_user:
        # Check if membership already exists
        existing_membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace_id, user_id=invited_user.user_id
        ).first()
        if existing_membership:
            flash(
                "That user is already in the workspace or has been invited.", "warning"
            )
            return redirect(
                url_for("workspace_bp.view_workspace", workspace_id=workspace_id)
            )

        # Create membership with status 'invited'
        new_membership = WorkspaceMember()
        new_membership.workspace_id = workspace_id
        new_membership.user_id = invited_user.user_id
        new_membership.role = "member"
        new_membership.status = "invited"
        db.session.add(new_membership)
        db.session.commit()

        # Create an Invitation record for existing users too
        invitation_token = secrets.token_urlsafe(32)
        invitation = Invitation()
        invitation.token = invitation_token
        invitation.workspace_id = workspace_id
        invitation.inviter_id = current_user.user_id
        invitation.email = email
        invitation.custom_message = custom_message
        db.session.add(invitation)
        db.session.commit()

        # Build a link to the respond invitation page
        invitation_link = url_for(
            "workspace_bp.respond_invitation",
            invitation_id=invitation.id,
            _external=True,
        )
        email_body = f"""
        <p>Hello {invited_user.first_name or invited_user.username},</p>
        <p>You have been invited to join the workspace <strong>{workspace.name}</strong> on {APP_NAME}.</p>
        <p>{custom_message}</p>
        <p>Please click the link below to accept or decline the invitation:</p>
        <p><a href="{invitation_link}">Respond to Invitation</a></p>
        """
        send_email(
            to_address=email,
            subject=f"Invitation to Join {APP_NAME} Workspace",
            body_html=email_body,
        )
        flash(f"Invitation sent to {email}.", "success")
    else:
        # For users who do not exist, create an Invitation record and send registration link.
        invitation = Invitation()
        invitation.token = invitation_token
        invitation.workspace_id = workspace_id
        invitation.email = email
        invitation.inviter_id = current_user.user_id
        invitation.custom_message = custom_message

        db.session.add(invitation)
        db.session.commit()

        registration_link = url_for(
            "auth_bp.register",
            invitation_token=invitation_token,
            workspace_id=workspace_id,
            _external=True,
        )
        email_body = f"""
        <p>Hello,</p>
        <p>You have been invited to join the workspace <strong>{workspace.name}</strong> on {APP_NAME}.</p>
        <p>{custom_message}</p>
        <p>Please register using the following link. Once you register, you'll be automatically added to the workspace:</p>
        <p><a href="{registration_link}">Register on {APP_NAME}</a></p>
        """
        send_email(
            to_address=email,
            subject=f"Invitation to Join {APP_NAME} Workspace",
            body_html=email_body,
        )
        flash(f'Invitation email sent to {email}. <a target="_blank" href="https://ai.broadcomms.net/webmail">Check Email</a>', "success")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

###################################
# 5a. Respond to Workspace Invitation (NEW)
###################################
@workspace_bp.route("/invitation/<int:invitation_id>", methods=["GET"]) 
@login_required
def respond_invitation(invitation_id):
    """Allow an invited (existing) user to accept or decline a workspace invitation.

    The invite is addressed to a specific email. We require the logged-in user
    to match that email. Action can be provided via ?action=accept or ?action=decline;
    if not provided, render a small confirmation page.
    """
    from datetime import datetime
    invitation = Invitation.query.get_or_404(invitation_id)

    # Basic validity: still pending and not expired (if expiration set)
    is_not_expired = (
        invitation.expiration_timestamp is None
        or invitation.expiration_timestamp > datetime.utcnow()
    )
    if invitation.status != "pending" or not is_not_expired:
        flash("This invitation is no longer valid.", "warning")
        return redirect(url_for("account_bp.account"))

    # Only the intended recipient can act on it
    if (current_user.email or "").lower() != (invitation.email or "").lower():
        flash("This invitation is for a different user.", "danger")
        return redirect(url_for("account_bp.account"))

    action = request.args.get("action")  # accept | decline | None

    # Find or create the membership record depending on action
    membership = WorkspaceMember.query.filter_by(
        workspace_id=invitation.workspace_id, user_id=current_user.user_id
    ).first()

    if action == "accept":
        if not membership:
            membership = WorkspaceMember()
            membership.workspace_id = invitation.workspace_id
            membership.user_id = current_user.user_id
            membership.role = "member"
            membership.status = "active"
            membership.joined_timestamp = datetime.utcnow()
            db.session.add(membership)
        else:
            # Activate if invited/requested; keep as-is if already active
            if membership.status != "active":
                membership.status = "active"
                membership.joined_timestamp = datetime.utcnow()

        # Mark invitation as accepted (or delete it)
        try:
            db.session.delete(invitation)
            db.session.commit()
            flash("Invitation accepted. You've joined the workspace.", "success")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error accepting workspace invitation {invitation_id}: {e}")
            flash("Error processing invitation.", "danger")
            return redirect(url_for("account_bp.account"))

        return redirect(url_for("workspace_bp.view_workspace", workspace_id=membership.workspace_id))

    elif action == "decline":
        try:
            # If we had created a placeholder invited membership, remove it
            if membership and membership.status != "active":
                db.session.delete(membership)
            invitation.status = "declined"
            db.session.commit()
            flash("Invitation declined.", "info")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error declining workspace invitation {invitation_id}: {e}")
            flash("Error processing invitation.", "danger")
        return redirect(url_for("account_bp.account"))

    # No action provided: render a simple confirmation page
    workspace = db.session.get(Workspace, invitation.workspace_id)
    return render_template(
        "workspace_respond_invitation.html",
        invitation=invitation,
        workspace=workspace,
    )

###################################
# 6. Request to Join Workspace
###################################
@workspace_bp.route("/request_join/<int:workspace_id>", methods=["POST"])
@login_required
def request_join(workspace_id):
    """
    Handles requests to join a public workspace.
    """
    workspace = Workspace.query.get_or_404(workspace_id)

    # Check if the user is already a member
    existing_member = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id
    ).first()
    if existing_member:
        flash("You are already a member of this workspace.", "info")
        return redirect(url_for("workspace_bp.list_workspaces"))

    # Add the user as a pending member
    new_member = WorkspaceMember()
    new_member.workspace_id = workspace_id
    new_member.user_id = current_user.user_id
    new_member.role = "member"
    new_member.status = "requested"
    db.session.add(new_member)
    db.session.commit()

    flash("Your request to join the workspace has been sent.", "success")
    return redirect(url_for("workspace_bp.list_workspaces"))

###################################
# 7. Approve Member Request/Invite
###################################
@workspace_bp.route("/<int:workspace_id>/members/<int:member_id>/approve", methods=["POST"])
@login_required
def approve_member(workspace_id, member_id):
    """Approves a pending member (status 'requested' or 'invited')."""
    if not check_admin_permission(workspace_id, current_user.user_id):
        flash("You don't have permission to manage members.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    member = WorkspaceMember.query.filter_by(id=member_id, workspace_id=workspace_id).first_or_404()

    if member.status not in ['requested', 'invited']:
        flash("This member is not pending approval.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    try:
        member.status = 'active'
        member.joined_timestamp = datetime.utcnow() # Set join time on approval
        db.session.commit()
        flash(f"Member {member.user.email} approved.", "success")
        # TODO: Optionally send an email notification to the approved user
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error approving member {member_id} in workspace {workspace_id}: {e}")
        flash("An error occurred while approving the member.", "danger")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

###################################
# 8. Reject Member Request/Invite
###################################
@workspace_bp.route("/<int:workspace_id>/members/<int:member_id>/reject", methods=["POST"])
@login_required
def reject_member(workspace_id, member_id):
    """Rejects a pending member (status 'requested' or 'invited') by deleting the record."""
    if not check_admin_permission(workspace_id, current_user.user_id):
        flash("You don't have permission to manage members.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    member = WorkspaceMember.query.filter_by(id=member_id, workspace_id=workspace_id).first_or_404()

    if member.status not in ['requested', 'invited']:
        flash("This member is not pending rejection.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    try:
        email = member.user.email # Get email before deleting
        db.session.delete(member)
        db.session.commit()
        flash(f"Membership request/invitation for {email} rejected.", "success")
        # TODO: Optionally send an email notification about the rejection
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error rejecting member {member_id} in workspace {workspace_id}: {e}")
        flash("An error occurred while rejecting the member.", "danger")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

###################################
# 9. Remove Active Member
###################################
@workspace_bp.route("/<int:workspace_id>/members/<int:member_id>/remove", methods=["POST"])
@login_required
def remove_member(workspace_id, member_id):
    """Removes an active member from the workspace."""
    workspace = Workspace.query.get_or_404(workspace_id) # Need workspace to check owner
    if not check_admin_permission(workspace_id, current_user.user_id):
        flash("You don't have permission to manage members.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    member = WorkspaceMember.query.filter_by(id=member_id, workspace_id=workspace_id).first_or_404()

    # Prevent removing the workspace owner
    if member.user_id == workspace.owner_id:
        flash("The workspace owner cannot be removed.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    # Prevent removing oneself (should leave workspace instead if that's implemented)
    if member.user_id == current_user.user_id:
        flash("You cannot remove yourself using this function.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    if member.status != 'active':
        flash("This member is not active and cannot be removed this way.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    try:
        email = member.user.email # Get email before deleting
        db.session.delete(member)
        db.session.commit()
        flash(f"Member {email} removed from the workspace.", "success")
        # TODO: Optionally send an email notification about the removal
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error removing member {member_id} from workspace {workspace_id}: {e}")
        flash("An error occurred while removing the member.", "danger")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))


###################################
# 10. Change Member Role
###################################
@workspace_bp.route("/<int:workspace_id>/members/<int:member_id>/change_role", methods=["POST"])
@login_required
def change_role(workspace_id, member_id):
    """Changes the role of an active member (e.g., member <-> manager)."""
    workspace = Workspace.query.get_or_404(workspace_id) # Need workspace to check owner

    # --- Permission Check: Only Owner or Admin can change roles ---
    # Using a stricter check here - maybe only owner/admin can promote/demote
    is_owner = workspace.owner_id == current_user.user_id
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id, status='active'
    ).first()
    is_admin = membership and membership.role == 'admin'

    if not (is_owner or is_admin):
        flash("You do not have permission to change member roles.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    member = WorkspaceMember.query.filter_by(id=member_id, workspace_id=workspace_id).first_or_404()
    new_role = request.form.get("new_role")

    # Validate new role
    allowed_roles = ['member', 'manager', 'admin'] # Define roles that can be assigned
    if new_role not in allowed_roles:
        flash("Invalid role specified.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    # Prevent changing the owner's role (owner should always be admin implicitly or explicitly)
    if member.user_id == workspace.owner_id:
        flash("The workspace owner's role cannot be changed.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    # Prevent non-owners from promoting someone to 'admin' (only owner can make admins)
    if new_role == 'admin' and not is_owner:
         flash("Only the workspace owner can assign the 'admin' role.", "danger")
         return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    # Prevent demoting the last admin if you are not the owner (optional rule)
    # if member.role == 'admin' and not is_owner:
    #    admin_count = WorkspaceMember.query.filter_by(workspace_id=workspace_id, role='admin', status='active').count()
    #    if admin_count <= 1:
    #         flash("Cannot remove the last admin.", "danger")
    #         return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    if member.status != 'active':
        flash("Cannot change role for inactive members.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    try:
        member.role = new_role
        db.session.commit()
        flash(f"Role for {member.user.email} updated to {new_role}.", "success")
        # TODO: Optionally send an email notification about the role change
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error changing role for member {member_id} in workspace {workspace_id}: {e}")
        flash("An error occurred while changing the role.", "danger")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))



