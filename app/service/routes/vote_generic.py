# app/service/routes/vote_generic.py
"""Generic voting payload generator.
Creates a vote task over clusters, ideas, or manual configured items.
Supports two-stage voting: clusters first, then ideas within top cluster.
"""
from __future__ import annotations

import json
from flask import current_app

from app.extensions import db
from app.models import BrainstormTask, Workshop, BrainstormIdea, IdeaCluster, WorkshopParticipant, WorkshopPlanItem
from app.tasks.registry import TASK_REGISTRY
from app.utils.value_parsing import bounded_int, safe_int


def _get_plan_item_config(workshop_id: int) -> dict | None:
    """Return config for the NEXT vote_generic plan item, preferring config_json.

    We select the first enabled item with order_index greater than the current_task_index,
    so multiple vote phases can have different configs (e.g., two-stage voting).
    """
    try:
        ws = db.session.get(Workshop, workshop_id)
    except Exception:
        ws = None
    try:
        current_idx = ws.current_task_index if ws and isinstance(ws.current_task_index, int) else -1
        q = (
            WorkshopPlanItem.query
            .filter_by(workshop_id=workshop_id, task_type="vote_generic", enabled=True)
            .order_by(WorkshopPlanItem.order_index.asc())
        )
        for item in q.all():
            try:
                if item.order_index is not None and int(item.order_index) <= current_idx:
                    continue
            except Exception:
                pass
            # Prefer config_json (already a JSON string) over description
            if getattr(item, 'config_json', None):
                try:
                    data = json.loads(item.config_json) if not isinstance(item.config_json, dict) else item.config_json
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
            if getattr(item, 'description', None):
                try:
                    data = json.loads(item.description)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
            # If we reached here, no usable config on this item; fall through to next
        return None
    except Exception:
        return None


def _collect_vote_items(workshop_id: int, cfg: dict) -> tuple[list[dict], str]:
    """Return (items, mode) where items is list of {id,label,type,description?} and mode is 'clusters'|'ideas'|'manual'."""
    # Two-stage flag: if cfg.stage == 'ideas_from_top_cluster', attempt to target ideas from most voted cluster
    stage = (cfg or {}).get("stage")  # None | 'ideas_from_top_cluster'

    # Try clusters from latest clustering_voting task
    latest_cluster_task = (
        BrainstormTask.query
        .filter_by(workshop_id=workshop_id, task_type="clustering_voting")
        .order_by(BrainstormTask.started_at.desc().nullslast(), BrainstormTask.id.desc())
        .first()
    )
    if latest_cluster_task and stage != "ideas":  # if explicitly 'ideas', skip clusters path
        clusters = IdeaCluster.query.filter_by(task_id=latest_cluster_task.id).all()
        if clusters:
            # If secondary stage: pick top cluster by vote count, then list its ideas
            if stage == "ideas_from_top_cluster":
                top = None
                for c in clusters:
                    # votes relationship may be dynamic; count efficiently
                    try:
                        vcount = int(c.votes.count())
                    except Exception:
                        vcount = 0
                    if (top is None) or (vcount > top[1]):
                        top = (c, vcount)
                if top and top[0]:
                    ideas = BrainstormIdea.query.filter_by(cluster_id=top[0].id).all()
                    if ideas:
                        return ([{"id": i.id, "label": i.content[:80], "type": "idea"} for i in ideas], "ideas")
            # Default: vote on clusters
            return ([{"id": c.id, "label": c.name, "type": "cluster", "description": c.description} for c in clusters], "clusters")

    # Else try ideas from latest brainstorming task (also used when stage == 'ideas')
    latest_brain_task = (
        BrainstormTask.query
        .filter_by(workshop_id=workshop_id, task_type="brainstorming")
        .order_by(BrainstormTask.started_at.desc().nullslast(), BrainstormTask.id.desc())
        .first()
    )
    if latest_brain_task:
        ideas = BrainstormIdea.query.filter_by(task_id=latest_brain_task.id).all()
        if ideas:
            return ([{"id": i.id, "label": i.content[:80], "type": "idea"} for i in ideas], "ideas")

    # Fallback to manual config
    manual_items = (cfg or {}).get("items")
    if isinstance(manual_items, list) and manual_items:
        # Ensure shape
        norm = []
        for idx, it in enumerate(manual_items):
            if isinstance(it, dict):
                label = it.get("label") or it.get("name") or f"Item {idx+1}"
                iid = it.get("id") or f"m{idx+1}"
                norm.append({"id": iid, "label": label, "type": "manual", "description": it.get("description")})
            elif isinstance(it, str):
                norm.append({"id": f"m{idx+1}", "label": it, "type": "manual"})
        if norm:
            return (norm, "manual")

    return ([], "manual")


def get_vote_generic_payload(workshop_id: int, phase_context: str | None = None):
    ws = db.session.get(Workshop, workshop_id)
    if not ws:
        return "Workshop not found", 404

    cfg = _get_plan_item_config(workshop_id) or {}
    items, mode = _collect_vote_items(workshop_id, cfg)

    # dots override
    dots_override = cfg.get("dots_per_user")
    default_dots = bounded_int(getattr(ws, 'dots_per_user', 5), default=5, minimum=1, maximum=100)
    if dots_override is not None:
        dots_per_user = bounded_int(dots_override, default=default_dots, minimum=1, maximum=100)
    else:
        dots_per_user = default_dots

    # Clarify title based on stage to avoid confusion when two vote steps exist
    stage = (cfg or {}).get("stage") or ("manual" if mode == "manual" else ("ideas" if mode == "ideas" else "clusters"))
    if stage == "ideas_from_top_cluster":
        title = "Vote: Ideas in Top Cluster"
    elif mode == "clusters" or stage == "clusters":
        title = "Vote: Clusters"
    elif mode == "ideas" or stage == "ideas":
        title = "Vote: Ideas"
    else:
        title = "Vote"
    task_description = "Cast your votes on the items listed. Use your dots wisely."
    instructions = "Click on an item to cast a dot. You can recast until time is up."
    duration = safe_int(TASK_REGISTRY.get("vote_generic", {}).get("default_duration", 600), default=600)

    payload = {
        "title": title,
        "task_type": "vote_generic",
        "task_description": task_description,
        "instructions": instructions,
        "task_duration": duration,
        "items": items,  # list of {id,label,type,description?}
        "mode": mode,    # 'clusters' | 'ideas' | 'manual'
        "sub_stage": stage,  # explicit stage for UI/analytics
        "dots_per_user": dots_per_user,
        "narration": "We’ll take a moment to vote. Use your dots to signal what you find most promising.",
        "tts_script": "We’re ready to vote. You’ll see a list of items on screen — click to place your dots on the options you believe we should prioritize. Use all your dots, and if you change your mind, you can adjust while the clock is running.",
        "tts_read_time_seconds": 40,
        "phase_context": phase_context or "",
    }

    # Create DB task
    task = BrainstormTask()
    task.workshop_id = workshop_id
    task.task_type = "vote_generic"
    task.title = payload["title"]
    task.description = payload.get("task_description")
    task.duration = safe_int(payload.get("task_duration", duration), default=duration)
    task.status = "pending"
    payload_str = json.dumps(payload)
    task.prompt = payload_str
    task.payload_json = payload_str
    db.session.add(task)
    db.session.flush()
    payload["task_id"] = task.id

    # Initialize dots for accepted participants
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, status="accepted").all()
    for p in participants:
        p.dots_remaining = dots_per_user
    payload["participants_dots"] = {part.user_id: dots_per_user for part in participants}

    current_app.logger.info(
        f"[VoteGeneric] Created task {task.id} for workshop {workshop_id} with {len(items)} items (mode={mode})"
    )
    return payload
