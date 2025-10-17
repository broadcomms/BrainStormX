"""Ideas gallery routes.

This module exposes a public-facing "innovation gallery" of finalized ideas
produced inside completed workshops.  It intentionally keeps the query logic
lightweight and performs some presentation-oriented aggregation in Python for
clarity and extensibility (e.g., later adding tags, sentiment, AI summaries).

URL Structure
-------------
GET /ideas                -> List (search / filter / sort / paginate)
GET /ideas/<idea_id>      -> Detail view

Access Control
--------------
Only ideas that originate from workshops in workspaces the current user is an
active member of (or created) are shown.  Only workshops with status
'completed' are considered "final" for the gallery.

Future Enhancements (not implemented yet but designed for):
- Tagging & keyword extraction
- AI-generated executive summaries per idea / cluster
- Export to PDF / report bundling
- Bookmark / favorite ideas
"""

from typing import Any, Dict, List, Set

from flask import Blueprint, render_template, request, abort, redirect, url_for, flash, Response
from flask_login import login_required, current_user
from sqlalchemy import func

from ...extensions import db
from ...models import (
    BrainstormIdea,
    BrainstormTask,
    Workshop,
    WorkspaceMember,
    IdeaCluster,
    IdeaVote,
    IdeaTag,
    FavoriteIdea,
)
from ...models import idea_tag_association  # Association table

ideas_bp = Blueprint("ideas_bp", __name__, template_folder="../../templates")


# ---------- Helper Utilities -------------------------------------------------
def _accessible_workspace_ids(user_id: int):
    return [
        wm.workspace_id
        for wm in WorkspaceMember.query.filter_by(user_id=user_id, status="active").all()
    ]


def _base_idea_query(user_id: int):
    """Return base query for finalized ideas accessible to the user."""
    ws_ids = _accessible_workspace_ids(user_id)
    if not ws_ids:
        # Empty list -> produce no results cleanly (use 0=1 for portability)
        from ...extensions import db as _db
        return BrainstormIdea.query.filter(_db.text('0=1'))

    # Keep query lean; eager loading omitted to satisfy strict type checking.
    q = (
        BrainstormIdea.query
        .join(BrainstormTask, BrainstormIdea.task_id == BrainstormTask.id)
        .join(Workshop, BrainstormTask.workshop_id == Workshop.id)
        .filter(Workshop.workspace_id.in_(ws_ids), Workshop.status == "completed")
    )
    return q


def _vote_count_map(cluster_ids):
    if not cluster_ids:
        return {}
    rows = (
        db.session.query(IdeaVote.cluster_id, func.count(IdeaVote.id))
        .filter(IdeaVote.cluster_id.in_(cluster_ids))
        .group_by(IdeaVote.cluster_id)
        .all()
    )
    return {cid: cnt for cid, cnt in rows}


# ---------- Routes -----------------------------------------------------------
@ideas_bp.route("/")
@login_required
def list_ideas():
    """List finalized ideas with search / filter / sort / pagination."""
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 50)
    q_param = request.args.get("q", "").strip()
    workspace_filter = request.args.get("workspace_id", type=int)
    sort = request.args.get("sort", "recent")  # recent|votes|workshop|cluster|favorites
    tag_filter = request.args.get("tag")
    favorites_only = request.args.get("favs") == "1"

    base_q = _base_idea_query(current_user.user_id)

    if q_param:
        like = f"%{q_param}%"
        base_q = base_q.filter(BrainstormIdea.content.ilike(like))

    if workspace_filter:
        base_q = base_q.filter(Workshop.workspace_id == workspace_filter)

    if tag_filter:
        base_q = (
            base_q
            .join(idea_tag_association, idea_tag_association.c.idea_id == BrainstormIdea.id)
            .join(IdeaTag, IdeaTag.id == idea_tag_association.c.tag_id)
            .filter(IdeaTag.name == tag_filter)
        )

    if favorites_only:
        base_q = base_q.join(FavoriteIdea, FavoriteIdea.idea_id == BrainstormIdea.id).filter(FavoriteIdea.user_id == current_user.user_id)

    ideas = base_q.all()

    # Aggregate / decorate
    cluster_ids = [i.cluster_id for i in ideas if i.cluster_id]
    vote_map = _vote_count_map(cluster_ids)
    decorated: List[Dict[str, Any]] = []
    for idea in ideas:
        task = idea.task
        workshop = task.workshop if task else None
        cluster = idea.cluster
        votes = vote_map.get(cluster.id, 0) if cluster else 0
        decorated.append(
            dict(
                id=idea.id,
                content=idea.content,
                preview=(idea.content[:220] + "…") if len(idea.content) > 220 else idea.content,
                timestamp=idea.timestamp,
                workshop_title=workshop.title if workshop else "(Unknown Workshop)",
                workshop_id=workshop.id if workshop else None,
                workspace_id=workshop.workspace_id if workshop else None,
                cluster_name=cluster.name if cluster else "Unclustered",
                cluster_id=cluster.id if cluster else None,
                votes=votes,
                _workshop_obj=workshop,  # internal helper reference
                _cluster_obj=cluster,    # internal helper reference
            )
        )

    # Favorites set for current user (ids) BEFORE sorting
    fav_ids = {fid.idea_id for fid in FavoriteIdea.query.filter_by(user_id=current_user.user_id).all()}
    for d in decorated:
        d["is_fav"] = d["id"] in fav_ids

    # Determine final representative ideas (winning clusters per workshop)
    # Strategy: pick cluster(s) with max votes in each workshop; choose earliest idea in that cluster as representative.
    winners_by_workshop: Dict[int, Set[int]] = {}
    # Build per-workshop clusters with votes
    per_ws_clusters: Dict[int, Dict[int, int]] = {}
    for d in decorated:
        ws = d["_workshop_obj"]
        cl = d["_cluster_obj"]
        if not ws or not cl:
            continue
        per_ws_clusters.setdefault(ws.id, {}).setdefault(cl.id, d["votes"])
        # Ensure votes captured even if some ideas weren't in vote_map
        if d["votes"] > per_ws_clusters[ws.id].get(cl.id, 0):
            per_ws_clusters[ws.id][cl.id] = d["votes"]
    # Compute winners per workshop (may be multiple in case of tie)
    for ws_id, cl_votes in per_ws_clusters.items():
        if not cl_votes:
            continue
        max_votes = max(cl_votes.values())
        winners_by_workshop[ws_id] = {cid for cid, v in cl_votes.items() if v == max_votes}

    # For each winning cluster, choose earliest idea in that cluster as the representative
    winner_idea_ids = set()
    for ws_id, cluster_ids in winners_by_workshop.items():
        for cid in cluster_ids:
            candidates = [d for d in decorated if d.get("cluster_id") == cid and d.get("workshop_id") == ws_id]
            if not candidates:
                continue
            candidates.sort(key=lambda x: x["timestamp"])  # earliest
            winner_idea_ids.add(candidates[0]["id"])

    # Filter to finalists only (if no winners found for any workshop, keep list empty by design)
    decorated = [d for d in decorated if d["id"] in winner_idea_ids]

    # Strip internal helper refs before rendering
    for d in decorated:
        d.pop("_workshop_obj", None)
        d.pop("_cluster_obj", None)

    # Sorting (Python-side; dataset expected modest; optimize later)
    if sort == "favorites":
        decorated.sort(key=lambda x: (x["is_fav"], x["timestamp"]), reverse=True)
    elif sort == "votes":
        decorated.sort(key=lambda x: (x["votes"], x["timestamp"]), reverse=True)
    elif sort == "workshop":
        decorated.sort(key=lambda x: (x["workshop_title"].lower(), -x["timestamp"].timestamp()))
    elif sort == "cluster":
        decorated.sort(key=lambda x: (x["cluster_name"].lower(), -x["timestamp"].timestamp()))
    else:  # recent
        decorated.sort(key=lambda x: x["timestamp"], reverse=True)

    total_count = len(decorated)
    # Pagination manually (since we sorted Python-side)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = decorated[start:end]
    total_pages = (total_count + per_page - 1) // per_page if per_page else 1

    # Stats (top-level metrics)
    workshop_ids = {d["workshop_id"] for d in decorated if d["workshop_id"]}
    cluster_ids_used = {d["cluster_id"] for d in decorated if d["cluster_id"]}
    avg_votes = 0
    if decorated:
        avg_votes = round(sum(d["votes"] for d in decorated) / len(decorated), 2)

    # Workspaces for filter dropdown
    accessible_ws_ids = _accessible_workspace_ids(current_user.user_id)
    from ...models import Workspace  # local import to avoid circular
    workspaces = Workspace.query.filter(Workspace.workspace_id.in_(accessible_ws_ids)).order_by(Workspace.name.asc()).all()

    # Tag cloud (top 20)
    tag_rows = []
    try:
        tag_rows = (
            db.session.query(IdeaTag.name, func.count(idea_tag_association.c.idea_id).label("cnt"))
            .join(idea_tag_association, IdeaTag.id == idea_tag_association.c.tag_id)
            .group_by(IdeaTag.name)
            .order_by(func.count(idea_tag_association.c.idea_id).desc())
            .limit(20)
            .all()
        )
    except Exception:
        tag_rows = []

    # Mark page items favorites (already computed above)
    for d in page_items:
        d["is_fav"] = d.get("is_fav", False)

    return render_template(
        "service_idea_list.html",
        ideas=page_items,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_count=total_count,
        q=q_param,
        sort=sort,
        workspace_filter=workspace_filter,
        workspaces=workspaces,
        stat_workshops=len(workshop_ids),
        stat_clusters=len(cluster_ids_used),
        stat_avg_votes=avg_votes,
        tag_filter=tag_filter,
        tag_cloud=tag_rows,
        favorites_only=favorites_only,
    )


@ideas_bp.route("/<int:idea_id>")
@login_required
def idea_detail(idea_id: int):
    """Detailed view for a single finalized idea."""
    base_q = _base_idea_query(current_user.user_id)
    idea = base_q.filter(BrainstormIdea.id == idea_id).first()
    if not idea:
        abort(404)

    task = idea.task
    workshop = task.workshop if task else None
    cluster = idea.cluster

    # Votes (cluster-level)
    vote_count = 0
    if cluster:
        vote_count = IdeaVote.query.filter_by(cluster_id=cluster.id).count()

    # Determine if this idea is a finalist (representative of a winning cluster in its workshop)
    is_finalist = False
    try:
        if workshop and cluster:
            # Collect clusters and votes for this workshop
            cl_rows = (
                db.session.query(IdeaVote.cluster_id, func.count(IdeaVote.id))
                .join(IdeaCluster, IdeaCluster.id == IdeaVote.cluster_id)
                .filter(IdeaCluster.task.has(workshop_id=workshop.id))
                .group_by(IdeaVote.cluster_id)
                .all()
            )
            cl_vote_map = {cid: cnt for cid, cnt in cl_rows}
            if cl_vote_map:
                max_votes = max(cl_vote_map.values())
                winning_clusters = {cid for cid, v in cl_vote_map.items() if v == max_votes}
                if cluster.id in winning_clusters:
                    # Representative: earliest idea in the winning cluster
                    rep = (
                        BrainstormIdea.query
                        .filter_by(cluster_id=cluster.id)
                        .order_by(BrainstormIdea.timestamp.asc())
                        .first()
                    )
                    is_finalist = rep is not None and rep.id == idea.id
    except Exception:
        is_finalist = False

    # Related ideas (same cluster, excluding self)
    related = []
    if cluster:
        related = (
            BrainstormIdea.query.filter(
                BrainstormIdea.cluster_id == cluster.id,
                BrainstormIdea.id != idea.id,
            )
            .order_by(BrainstormIdea.timestamp.desc())
            .limit(8)
            .all()
        )

    return render_template(
        "service_idea_detail.html",
        idea=idea,
        workshop=workshop,
        cluster=cluster,
        vote_count=vote_count,
        related=related,
    is_finalist=is_finalist,
        is_favorited=FavoriteIdea.query.filter_by(user_id=current_user.user_id, idea_id=idea.id).first() is not None,
    )


@ideas_bp.route('/<int:idea_id>/favorite', methods=['POST'])
@login_required
def toggle_favorite(idea_id: int):
    base_q = _base_idea_query(current_user.user_id)
    idea = base_q.filter(BrainstormIdea.id == idea_id).first()
    if not idea:
        abort(404)
    existing = FavoriteIdea.query.filter_by(user_id=current_user.user_id, idea_id=idea_id).first()
    if existing:
        db.session.delete(existing)
        flash('Removed from favorites', 'info')
    else:
        fav = FavoriteIdea()
        fav.user_id = current_user.user_id
        fav.idea_id = idea_id
        db.session.add(fav)
        flash('Added to favorites', 'success')
    db.session.commit()
    return redirect(url_for('ideas_bp.idea_detail', idea_id=idea_id))


@ideas_bp.route('/export.csv')
@login_required
def export_ideas_csv():
    base_q = _base_idea_query(current_user.user_id)
    rows = base_q.all()
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Idea ID','Content','Workshop','Timestamp'])
    for r in rows:
        task = r.task
        workshop = task.workshop if task else None
        writer.writerow([r.id, r.content, getattr(workshop, 'title', ''), r.timestamp.isoformat()])
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=ideas_export.csv'})
