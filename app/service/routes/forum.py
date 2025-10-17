from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict
from urllib.parse import urlencode

from flask import Blueprint, jsonify, request, session
from flask_login import login_required, current_user
from sqlalchemy import func, or_

from app.extensions import db, socketio
from app.models import Workshop, WorkshopParticipant, User
from app.models_forum import ForumCategory, ForumTopic, ForumPost, ForumReply, ForumReaction
from app.forum.service import seed_forum_from_results


class TopicEnrichmentPayload(TypedDict):
    related_ideas: list[dict[str, Any]]
    tags: list[str]
    insights: list[str]


forum_bp = Blueprint("forum_bp", __name__)
def _acting_user_id() -> int | None:
    """Return the logged-in user id from session; fallback to current_user if available."""
    try:
        if session:
            uid_val = session.get('_user_id')
            if uid_val is not None:
                return int(uid_val)
    except Exception:
        pass
    try:
        return int(current_user.user_id)  # type: ignore
    except Exception:
        return None


def _user_in_workshop(workshop_id: int, user_id: int) -> bool:
    # Organizer or participant qualifies
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return False
    try:
        if int(ws.created_by_id) == int(user_id):
            return True
    except Exception:
        if ws.created_by_id == user_id:
            return True
    exists = (
        db.session.query(WorkshopParticipant.id)
        .filter(
            WorkshopParticipant.workshop_id == int(workshop_id),
            WorkshopParticipant.user_id == int(user_id),
        )
        .first()
    )
    return exists is not None


def _is_organizer(workshop_id: int, user_id: int) -> bool:
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return False
    try:
        if int(ws.created_by_id) == int(user_id):
            return True
    except Exception:
        if ws.created_by_id == user_id:
            return True
    # Fall back to participant role check
    try:
        part = (
            db.session.query(WorkshopParticipant.id)
            .filter(
                WorkshopParticipant.workshop_id == workshop_id,
                WorkshopParticipant.user_id == user_id,
                WorkshopParticipant.role == 'organizer',
            )
            .first()
        )
        return part is not None
    except Exception:
        return False


def _user_label(u: User | None) -> str:
    if not u:
        return "User"
    try:
        return u.display_name
    except Exception:
        return u.email or f"User{u.user_id}"


@login_required
@forum_bp.post("/api/workshops/<int:workshop_id>/forum/seed")
def forum_seed(workshop_id: int):
    if not _is_organizer(workshop_id, current_user.user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        res = seed_forum_from_results(workshop_id)
        # Optional broadcast to notify clients that forum content is available
        socketio.emit("forum_seed_done", {"workshop_id": workshop_id, **res}, to=f"workshop_room_{workshop_id}")
        return jsonify({"success": True, **res})
    except Exception as e:  # pragma: no cover
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@login_required
@forum_bp.get("/api/workshops/<int:workshop_id>/forum/categories")
def list_categories(workshop_id: int):
    if not _user_in_workshop(workshop_id, current_user.user_id):
        return jsonify({"error": "Forbidden"}), 403

    rows = (
        db.session.query(
            ForumCategory.id,
            ForumCategory.title,
            ForumCategory.description,
            func.count(ForumTopic.id).label("topic_count"),
        )
        .outerjoin(ForumTopic, ForumTopic.category_id == ForumCategory.id)
        .filter(ForumCategory.workshop_id == workshop_id)
        .group_by(ForumCategory.id)
        .order_by(ForumCategory.id.asc())
        .all()
    )
    data = [
        {
            "id": cid,
            "title": title,
            "description": desc,
            "topic_count": int(topic_count or 0),
        }
        for (cid, title, desc, topic_count) in rows
    ]
    return jsonify({"categories": data})


@login_required
@forum_bp.get("/api/workshops/<int:workshop_id>/forum/topics")
def list_topics(workshop_id: int):
    if not _user_in_workshop(workshop_id, current_user.user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        category_id = int(request.args.get("category_id", "0"))
    except Exception:
        return jsonify({"error": "Invalid category_id"}), 400

    # Validate category belongs to workshop
    cat = db.session.get(ForumCategory, category_id)
    if not cat or cat.workshop_id != workshop_id:
        return jsonify({"error": "Category not found"}), 404

    # Pagination params
    def _parse_pos_int(name: str, default: int, minimum: int = 0) -> int:
        try:
            v = int(request.args.get(name, default))
            return max(minimum, v)
        except Exception:
            return default

    limit = _parse_pos_int("limit", 25, 1)
    offset = _parse_pos_int("offset", 0, 0)

    base_q = (
        db.session.query(ForumTopic)
        .filter(ForumTopic.workshop_id == workshop_id, ForumTopic.category_id == category_id)
    )
    total = base_q.count()
    # Clamp offset to last page when content shrinks to avoid empty tail pages
    if total > 0 and offset >= total:
        last_off = ((total - 1) // limit) * limit
        offset = max(0, last_off)
    rows = (
        base_q
        .order_by(ForumTopic.pinned.desc(), ForumTopic.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    data = [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "idea_id": t.idea_id,
            "pinned": bool(getattr(t, "pinned", False)),
            "locked": bool(getattr(t, "locked", False)),
        }
        for t in rows
    ]

    # Link helpers
    def _link(off: int) -> str:
        qp = {
            "category_id": category_id,
            "limit": limit,
            "offset": max(0, off),
        }
        return f"{request.base_url}?{urlencode(qp)}"

    next_off = offset + limit
    prev_off = max(0, offset - limit)
    last_off = ((total - 1) // limit) * limit if total > 0 else 0
    links = {
        "self": _link(offset),
        "next": _link(next_off) if next_off < total else None,
        "prev": _link(prev_off) if offset > 0 else None,
        "last": _link(last_off) if total > limit else None,
    }
    pagination = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(data),
        "has_next": next_off < total,
        "has_prev": offset > 0,
    }

    return jsonify({
        "topics": data,
        "category": {"id": cat.id, "title": cat.title},
        "links": links,
        "pagination": pagination,
    })


@login_required
@forum_bp.get("/api/workshops/<int:workshop_id>/forum/topics/<int:topic_id>/posts")
def list_posts(workshop_id: int, topic_id: int):
    if not _user_in_workshop(workshop_id, current_user.user_id):
        return jsonify({"error": "Forbidden"}), 403

    topic = db.session.get(ForumTopic, topic_id)
    if not topic or topic.workshop_id != workshop_id:
        return jsonify({"error": "Topic not found"}), 404

    # Pagination params for posts
    def _parse_pos_int(name: str, default: int, minimum: int = 0) -> int:
        try:
            v = int(request.args.get(name, default))
            return max(minimum, v)
        except Exception:
            return default

    limit = _parse_pos_int("limit", 25, 1)
    offset = _parse_pos_int("offset", 0, 0)

    base_q = (
        db.session.query(ForumPost)
        .filter(ForumPost.topic_id == topic_id, ForumPost.workshop_id == workshop_id)
    )
    total = base_q.count()
    # Clamp offset to last page when content shrinks
    if total > 0 and offset >= total:
        last_off = ((total - 1) // limit) * limit
        offset = max(0, last_off)
    posts = (
        base_q
        .order_by(ForumPost.created_at.asc(), ForumPost.id.asc())  # deterministic tie-breaker
        .offset(offset)
        .limit(limit)
        .all()
    )
    # Preload users
    user_ids = {p.user_id for p in posts}
    users = {u.user_id: u for u in db.session.query(User).filter(User.user_id.in_(user_ids)).all()} if user_ids else {}
    # Fetch replies for these posts
    post_ids = [p.id for p in posts]
    replies = []
    if post_ids:
        replies = (
            db.session.query(ForumReply)
            .filter(ForumReply.post_id.in_(post_ids), ForumReply.workshop_id == workshop_id)
            .order_by(ForumReply.created_at.asc())
            .all()
        )
    # Preload reply users
    reply_user_ids = {r.user_id for r in replies}
    if reply_user_ids:
        users.update({u.user_id: u for u in db.session.query(User).filter(User.user_id.in_(reply_user_ids)).all()})

    # Reactions aggregation for these posts and replies
    reactions_by_post: Dict[int, Dict[str, List[int]]] = {}
    reactions_by_reply: Dict[int, Dict[str, List[int]]] = {}
    if post_ids:
        reply_ids = [r.id for r in replies] if replies else []
        q = db.session.query(ForumReaction).filter(ForumReaction.workshop_id == workshop_id)
        conditions = []
        if post_ids:
            conditions.append(ForumReaction.post_id.in_(post_ids))
        if reply_ids:
            conditions.append(ForumReaction.reply_id.in_(reply_ids))
        if conditions:
            if len(conditions) == 1:
                q = q.filter(conditions[0])
            else:
                q = q.filter(or_(*conditions))
        reaction_rows = q.all()
        # collect user ids to enrich names
        react_user_ids = set()
        for fr in reaction_rows:
            react_user_ids.add(fr.user_id)
            if fr.post_id:
                m = reactions_by_post.setdefault(fr.post_id, {})
                m.setdefault(fr.reaction, []).append(fr.user_id)
            if fr.reply_id:
                m = reactions_by_reply.setdefault(fr.reply_id, {})
                m.setdefault(fr.reaction, []).append(fr.user_id)
        if react_user_ids:
            users.update({u.user_id: u for u in db.session.query(User).filter(User.user_id.in_(react_user_ids)).all()})

    # Group replies by post
    replies_by_post: Dict[int, List[ForumReply]] = {}
    for r in replies:
        replies_by_post.setdefault(r.post_id, []).append(r)

    def summarize_reactions(map_kind_users: Dict[str, List[int]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for kind, uids in (map_kind_users or {}).items():
            names: List[str] = []
            for uid in uids:
                names.append(_user_label(users.get(uid)))
            out[kind] = {"count": len(uids), "users": names, "user_ids": list(uids)}
        return out

    def post_dict(p: ForumPost) -> Dict[str, Any]:
        return {
            "id": p.id,
            "user_id": p.user_id,
            "user_name": _user_label(users.get(p.user_id)),
            "body": p.body,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "edited_at": p.edited_at.isoformat() if getattr(p, "edited_at", None) else None,
            "reactions": summarize_reactions(reactions_by_post.get(p.id, {})),
            "reply_count": len(replies_by_post.get(p.id, []) or []),
            "replies": [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "user_name": _user_label(users.get(r.user_id)),
                    "body": r.body,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "edited_at": r.edited_at.isoformat() if getattr(r, "edited_at", None) else None,
                    "reactions": summarize_reactions(reactions_by_reply.get(r.id, {})),
                }
                for r in replies_by_post.get(p.id, [])
            ],
        }

    # Pagination links
    def _link(off: int) -> str:
        qp = {
            "limit": limit,
            "offset": max(0, off),
        }
        return f"{request.base_url}?{urlencode(qp)}"

    next_off = offset + limit
    prev_off = max(0, offset - limit)
    last_off = ((total - 1) // limit) * limit if total > 0 else 0
    links = {
        "self": _link(offset),
        "next": _link(next_off) if next_off < total else None,
        "prev": _link(prev_off) if offset > 0 else None,
        "last": _link(last_off) if total > limit else None,
    }
    pagination = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(posts),
        "has_next": next_off < total,
        "has_prev": offset > 0,
    }

    return jsonify({
        "topic": {"id": topic.id, "title": topic.title, "pinned": bool(getattr(topic, "pinned", False)), "locked": bool(getattr(topic, "locked", False))},
        "posts": [post_dict(p) for p in posts],
        "links": links,
        "pagination": pagination,
    })


@login_required
@forum_bp.get("/api/workshops/<int:workshop_id>/forum/posts/<int:post_id>/replies")
def list_post_replies(workshop_id: int, post_id: int):
    """Paginate replies for a single post."""
    if not _user_in_workshop(workshop_id, current_user.user_id):
        return jsonify({"error": "Forbidden"}), 403

    post = db.session.get(ForumPost, post_id)
    if not post or post.workshop_id != workshop_id:
        return jsonify({"error": "Post not found"}), 404

    def _parse_pos_int(name: str, default: int, minimum: int = 0) -> int:
        try:
            v = int(request.args.get(name, default))
            return max(minimum, v)
        except Exception:
            return default

    limit = _parse_pos_int("limit", 25, 1)
    offset = _parse_pos_int("offset", 0, 0)

    base_q = (
        db.session.query(ForumReply)
        .filter(ForumReply.post_id == post_id, ForumReply.workshop_id == workshop_id)
    )
    total = base_q.count()
    # Clamp offset to last page
    if total > 0 and offset >= total:
        last_off = ((total - 1) // limit) * limit
        offset = max(0, last_off)

    replies = (
        base_q
        .order_by(ForumReply.created_at.asc(), ForumReply.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    # Preload users
    reply_user_ids = {r.user_id for r in replies}
    users = {u.user_id: u for u in db.session.query(User).filter(User.user_id.in_(reply_user_ids)).all()} if reply_user_ids else {}

    # Reactions for these replies
    reactions_by_reply: Dict[int, Dict[str, List[int]]] = {}
    if replies:
        reply_ids = [r.id for r in replies]
        q = db.session.query(ForumReaction).filter(ForumReaction.workshop_id == workshop_id)
        q = q.filter(ForumReaction.reply_id.in_(reply_ids))
        reaction_rows = q.all()
        react_user_ids = set()
        for fr in reaction_rows:
            if fr.reply_id:
                m = reactions_by_reply.setdefault(fr.reply_id, {})
                m.setdefault(fr.reaction, []).append(fr.user_id)
                react_user_ids.add(fr.user_id)
        if react_user_ids:
            users.update({u.user_id: u for u in db.session.query(User).filter(User.user_id.in_(react_user_ids)).all()})

    def _user_label_local(u: Optional[User]) -> str:
        if not u:
            return "User"
        name = getattr(u, "display_name", None) or getattr(u, "name", None) or getattr(u, "email", None) or f"User {u.user_id}"
        return name

    def summarize_reactions_local(map_kind_users: Dict[str, List[int]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for kind, uids in (map_kind_users or {}).items():
            names: List[str] = []
            for uid in uids:
                names.append(_user_label_local(users.get(uid)))
            out[kind] = {"count": len(uids), "users": names, "user_ids": list(uids)}
        return out

    out_replies = [
        {
            "id": r.id,
            "user_id": r.user_id,
            "user_name": _user_label_local(users.get(r.user_id)),
            "body": r.body,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "edited_at": r.edited_at.isoformat() if getattr(r, "edited_at", None) else None,
            "reactions": summarize_reactions_local(reactions_by_reply.get(r.id, {})),
        }
        for r in replies
    ]

    def _link(off: int) -> str:
        qp = {
            "limit": limit,
            "offset": max(0, off),
        }
        return f"{request.base_url}?{urlencode(qp)}"

    next_off = offset + limit
    prev_off = max(0, offset - limit)
    last_off = ((total - 1) // limit) * limit if total > 0 else 0
    links = {
        "self": _link(offset),
        "next": _link(next_off) if next_off < total else None,
        "prev": _link(prev_off) if offset > 0 else None,
        "last": _link(last_off) if total > limit else None,
    }
    pagination = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(out_replies),
        "has_next": next_off < total,
        "has_prev": offset > 0,
    }

    return jsonify({
        "post": {"id": post.id, "topic_id": post.topic_id},
        "replies": out_replies,
        "links": links,
        "pagination": pagination,
    })


# --- Moderation & Editing Endpoints ---

@login_required
@forum_bp.patch("/api/workshops/<int:workshop_id>/forum/topics/<int:topic_id>")
def update_topic(workshop_id: int, topic_id: int):
    uid = _acting_user_id()
    if uid is None or not _user_in_workshop(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    topic = db.session.get(ForumTopic, topic_id)
    if not topic or topic.workshop_id != workshop_id:
        return jsonify({"error": "Not found"}), 404
    payload = request.get_json(silent=True) or {}
    # Organizer-only fields
    if "pinned" in payload or "locked" in payload:
        # Inline organizer check via workshop creator or organizer participant role
        is_org = False
        ws = db.session.get(Workshop, workshop_id)
        if ws and uid is not None and int(ws.created_by_id) == int(uid):
            is_org = True
        else:
            part = (
                db.session.query(WorkshopParticipant.id)
                .filter(
                    WorkshopParticipant.workshop_id == int(workshop_id),
                    WorkshopParticipant.user_id == int(uid or -1),
                    WorkshopParticipant.role == 'organizer',
                )
                .first()
            )
            is_org = part is not None
        if not is_org:
            return jsonify({"error": "Forbidden"}), 403
        if "pinned" in payload:
            topic.pinned = bool(payload.get("pinned"))
        if "locked" in payload:
            topic.locked = bool(payload.get("locked"))
    # Title/description can be edited by organizer or original author (if present)
    can_edit = (uid is not None and _is_organizer(workshop_id, uid)) or (uid is not None and int(topic.user_id) == int(uid))
    if not can_edit and ("title" in payload or "description" in payload):
        return jsonify({"error": "Forbidden"}), 403
    if "title" in payload:
        topic.title = (payload.get("title") or topic.title).strip()[:250]
    if "description" in payload:
        topic.description = (payload.get("description") or None)
    db.session.commit()
    return jsonify({"success": True, "topic": {"id": topic.id, "title": topic.title, "description": topic.description, "pinned": topic.pinned, "locked": topic.locked}})


@login_required
@forum_bp.delete("/api/workshops/<int:workshop_id>/forum/topics/<int:topic_id>")
def delete_topic(workshop_id: int, topic_id: int):
    uid = _acting_user_id()
    if uid is None or not _is_organizer(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    topic = db.session.get(ForumTopic, topic_id)
    if not topic or topic.workshop_id != workshop_id:
        return jsonify({"error": "Not found"}), 404
    try:
        db.session.delete(topic)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:  # pragma: no cover
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@login_required
@forum_bp.patch("/api/workshops/<int:workshop_id>/forum/posts/<int:post_id>")
def update_post(workshop_id: int, post_id: int):
    uid = _acting_user_id()
    if uid is None or not _user_in_workshop(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    post = db.session.get(ForumPost, post_id)
    if not post or post.workshop_id != workshop_id:
        return jsonify({"error": "Not found"}), 404
    # Author or organizer can edit
    ws = db.session.get(Workshop, workshop_id)
    is_org = bool(ws and uid is not None and int(ws.created_by_id) == int(uid))
    if not (is_org or (uid is not None and int(post.user_id) == int(uid))):
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Body is required"}), 400
    post.body = body
    try:
        post.edited_at = func.now()
    except Exception:
        pass
    db.session.commit()
    return jsonify({"success": True})


@login_required
@forum_bp.delete("/api/workshops/<int:workshop_id>/forum/posts/<int:post_id>")
def delete_post(workshop_id: int, post_id: int):
    uid = _acting_user_id()
    if uid is None or not _user_in_workshop(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    post = db.session.get(ForumPost, post_id)
    if not post or post.workshop_id != workshop_id:
        return jsonify({"error": "Not found"}), 404
    ws = db.session.get(Workshop, workshop_id)
    is_org = bool(ws and uid is not None and int(ws.created_by_id) == int(uid))
    if not (is_org or (uid is not None and int(post.user_id) == int(uid))):
        return jsonify({"error": "Forbidden"}), 403
    try:
        db.session.delete(post)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:  # pragma: no cover
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@login_required
@forum_bp.patch("/api/workshops/<int:workshop_id>/forum/replies/<int:reply_id>")
def update_reply(workshop_id: int, reply_id: int):
    uid = _acting_user_id()
    if uid is None or not _user_in_workshop(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    reply = db.session.get(ForumReply, reply_id)
    if not reply or reply.workshop_id != workshop_id:
        return jsonify({"error": "Not found"}), 404
    ws = db.session.get(Workshop, workshop_id)
    is_org = bool(ws and uid is not None and int(ws.created_by_id) == int(uid))
    if not (is_org or (uid is not None and int(reply.user_id) == int(uid))):
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Body is required"}), 400
    reply.body = body
    try:
        reply.edited_at = func.now()
    except Exception:
        pass
    db.session.commit()
    return jsonify({"success": True})


@login_required
@forum_bp.delete("/api/workshops/<int:workshop_id>/forum/replies/<int:reply_id>")
def delete_reply(workshop_id: int, reply_id: int):
    uid = _acting_user_id()
    if uid is None or not _user_in_workshop(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    reply = db.session.get(ForumReply, reply_id)
    if not reply or reply.workshop_id != workshop_id:
        return jsonify({"error": "Not found"}), 404
    ws = db.session.get(Workshop, workshop_id)
    is_org = bool(ws and uid is not None and int(ws.created_by_id) == int(uid))
    if not (is_org or (uid is not None and int(reply.user_id) == int(uid))):
        return jsonify({"error": "Forbidden"}), 403
    try:
        db.session.delete(reply)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:  # pragma: no cover
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


# Reactions toggle
@login_required
@forum_bp.post("/api/workshops/<int:workshop_id>/forum/reactions")
def toggle_reaction(workshop_id: int):
    uid = _acting_user_id()
    if uid is None or not _user_in_workshop(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    post_id = payload.get("post_id")
    reply_id = payload.get("reply_id")
    reaction = (payload.get("reaction") or "like").strip().lower()[:32]
    if not post_id and not reply_id:
        return jsonify({"error": "post_id or reply_id required"}), 400
    # Ensure target exists and belongs to workshop
    if post_id:
        post = db.session.get(ForumPost, int(post_id))
        if not post or post.workshop_id != workshop_id:
            return jsonify({"error": "Not found"}), 404
    if reply_id:
        reply = db.session.get(ForumReply, int(reply_id))
        if not reply or reply.workshop_id != workshop_id:
            return jsonify({"error": "Not found"}), 404
    try:
        existing = (
            db.session.query(ForumReaction)
            .filter(ForumReaction.workshop_id == workshop_id,
                    ForumReaction.user_id == int(uid),
                    ForumReaction.reaction == reaction,
                    ForumReaction.post_id == (int(post_id) if post_id else None),
                    ForumReaction.reply_id == (int(reply_id) if reply_id else None))
            .first()
        )
        if existing:
            db.session.delete(existing)
            db.session.commit()
            # Broadcast live update
            socketio.emit("forum_reaction_updated", {
                "workshop_id": workshop_id,
                "post_id": int(post_id) if post_id else None,
                "reply_id": int(reply_id) if reply_id else None,
                "reaction": reaction,
                "toggled": "off",
                "user_id": int(uid),
            }, to=f"workshop_room_{workshop_id}")
            return jsonify({"success": True, "toggled": "off"})
        row = ForumReaction()
        row.workshop_id = workshop_id
        row.user_id = int(uid)
        row.reaction = reaction
        if post_id:
            row.post_id = int(post_id)
        if reply_id:
            row.reply_id = int(reply_id)
        db.session.add(row)
        db.session.commit()
        # Broadcast live update
        socketio.emit("forum_reaction_updated", {
            "workshop_id": workshop_id,
            "post_id": int(post_id) if post_id else None,
            "reply_id": int(reply_id) if reply_id else None,
            "reaction": reaction,
            "toggled": "on",
            "user_id": int(uid),
        }, to=f"workshop_room_{workshop_id}")
        return jsonify({"success": True, "toggled": "on"})
    except Exception as e:  # pragma: no cover
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


# Topic enrichment: basic related items stub (ideas/tags to be expanded)
@login_required
@forum_bp.get("/api/workshops/<int:workshop_id>/forum/topics/<int:topic_id>/enrichment")
def topic_enrichment(workshop_id: int, topic_id: int):
    if not _user_in_workshop(workshop_id, current_user.user_id):
        return jsonify({"error": "Forbidden"}), 403
    topic = db.session.get(ForumTopic, topic_id)
    if not topic or topic.workshop_id != workshop_id:
        return jsonify({"error": "Not found"}), 404
    # For now, provide a simple stub with potential hooks (ideas by idea_id, tags TBD)
    enrichment: TopicEnrichmentPayload = {"related_ideas": [], "tags": [], "insights": []}
    try:
        # If idea_id present, include the referenced idea content
        if topic.idea_id:
            from app.models import BrainstormIdea  # local import to avoid cycles
            idea = db.session.get(BrainstormIdea, int(topic.idea_id))
            if idea:
                enrichment["related_ideas"].append({
                    "id": idea.id,
                    "content": idea.content,
                })
    except Exception:
        pass
    return jsonify({"topic": {"id": topic.id, "title": topic.title}, "enrichment": enrichment})


@login_required
@forum_bp.post("/api/workshops/<int:workshop_id>/forum/posts")
def create_post(workshop_id: int):
    uid = _acting_user_id()
    if uid is None or not _user_in_workshop(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    topic_id = payload.get("topic_id")
    body = (payload.get("body") or "").strip()
    if not topic_id or not body:
        return jsonify({"error": "topic_id and body are required"}), 400
    topic = db.session.get(ForumTopic, int(topic_id))
    if not topic or topic.workshop_id != workshop_id:
        return jsonify({"error": "Topic not found"}), 404
    if getattr(topic, "locked", False):
        return jsonify({"error": "Topic is locked"}), 403

    row = ForumPost()
    row.workshop_id = workshop_id
    row.topic_id = topic.id
    row.user_id = int(uid)
    row.body = body
    db.session.add(row)
    db.session.commit()

    out = {
        "id": row.id,
        "topic_id": topic.id,
        "user_id": row.user_id,
        "user_name": _user_label(db.session.get(User, row.user_id)),
        "body": row.body,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    socketio.emit("forum_post_created", {"workshop_id": workshop_id, **out}, to=f"workshop_room_{workshop_id}")
    return jsonify({"success": True, "post": out})


@login_required
@forum_bp.post("/api/workshops/<int:workshop_id>/forum/replies")
def create_reply(workshop_id: int):
    uid = _acting_user_id()
    if uid is None or not _user_in_workshop(workshop_id, uid):
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    post_id = payload.get("post_id")
    body = (payload.get("body") or "").strip()
    if not post_id or not body:
        return jsonify({"error": "post_id and body are required"}), 400
    post = db.session.get(ForumPost, int(post_id))
    if not post or post.workshop_id != workshop_id:
        return jsonify({"error": "Post not found"}), 404
    # Enforce topic lock
    try:
        parent_topic = db.session.get(ForumTopic, int(post.topic_id)) if post and post.topic_id else None
    except Exception:
        parent_topic = None
    if parent_topic and getattr(parent_topic, "locked", False):
        return jsonify({"error": "Topic is locked"}), 403

    row = ForumReply()
    row.workshop_id = workshop_id
    row.post_id = post.id
    row.user_id = int(uid)
    row.body = body
    db.session.add(row)
    db.session.commit()

    out = {
        "id": row.id,
        "post_id": post.id,
        "user_id": row.user_id,
        "user_name": _user_label(db.session.get(User, row.user_id)),
        "body": row.body,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    socketio.emit("forum_reply_created", {"workshop_id": workshop_id, **out}, to=f"workshop_room_{workshop_id}")
    return jsonify({"success": True, "reply": out})
