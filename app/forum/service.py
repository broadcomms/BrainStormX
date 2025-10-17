# app/forum/service.py
from typing import Dict
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from app.extensions import db
from app.models_forum import ForumCategory, ForumTopic
from app.models import IdeaCluster, BrainstormIdea, IdeaVote, BrainstormTask

def seed_forum_from_results(workshop_id: int) -> Dict[str, int]:
    """Create forum categories from clusters and topics from ideas.
    Idempotent via unique constraints (workshop_id, cluster_id) and (workshop_id, category_id, idea_id).
    Returns counts of created entities.
    """
    created_cats = 0
    created_topics = 0

    # Order clusters by votes desc, then id
    clusters = (
        db.session.query(IdeaCluster, func.count(IdeaVote.id).label('vote_count'))
        .outerjoin(IdeaVote, IdeaCluster.id == IdeaVote.cluster_id)
        .join(BrainstormTask, BrainstormTask.id == IdeaCluster.task_id)
        .filter(BrainstormTask.workshop_id == workshop_id)
        .group_by(IdeaCluster.id)
        .order_by(func.count(IdeaVote.id).desc(), IdeaCluster.id.asc())
        .all()
    )

    # Map cluster_id -> category
    cat_by_cluster: Dict[int, ForumCategory] = {}
    for cluster, _cnt in clusters:
        # Upsert-ish: try get existing
        cat = ForumCategory.query.filter_by(workshop_id=workshop_id, cluster_id=cluster.id).first()
        if not cat:
            cat = ForumCategory()  # avoid kwargs to satisfy type checkers
            cat.workshop_id = workshop_id
            cat.title = cluster.name or f"Cluster {cluster.id}"
            cat.description = cluster.description or None
            cat.cluster_id = cluster.id
            db.session.add(cat)
            try:
                db.session.flush()
                created_cats += 1
            except IntegrityError:
                db.session.rollback()
                # Created in a race by another worker; fetch it
                cat = ForumCategory.query.filter_by(workshop_id=workshop_id, cluster_id=cluster.id).first() or cat
        cat_by_cluster[cluster.id] = cat

    # Topics from ideas by cluster
    ideas = (
        db.session.query(BrainstormIdea)
        .join(IdeaCluster, BrainstormIdea.cluster_id == IdeaCluster.id)
        .join(BrainstormTask, BrainstormTask.id == IdeaCluster.task_id)
        .filter(BrainstormTask.workshop_id == workshop_id)
        .order_by(BrainstormIdea.id.asc())
        .all()
    )
    for idea in ideas:
        cluster_id = getattr(idea, 'cluster_id', None)
        if cluster_id is None:
            continue
        cat = cat_by_cluster.get(cluster_id)
        if not cat:
            continue
        existing = ForumTopic.query.filter_by(workshop_id=workshop_id, category_id=cat.id, idea_id=idea.id).first()
        if existing:
            continue
        topic = ForumTopic()
        topic.workshop_id = workshop_id
        topic.category_id = cat.id
        # Topic creator is unknown during seeding; leave null
        topic.title = (idea.content[:200] if idea.content else f"Idea {idea.id}")
        topic.description = idea.content or None  # Optionally enrich with feasibility summary later
        topic.idea_id = idea.id
        db.session.add(topic)
        try:
            db.session.flush()
            created_topics += 1
        except IntegrityError:
            db.session.rollback()
            # Likely created concurrently; ignore and continue

    db.session.commit()
    return {"categories": created_cats, "topics": created_topics}
