from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import func

from app.assistant.tools.base import BaseTool
from app.assistant.tools.types import ToolResult, ToolSchema
from app.models import BrainstormIdea, BrainstormTask, IdeaCluster, IdeaVote, db


class ListClustersTool(BaseTool):
    """Return latest clustering_voting clusters for a workshop.

    Output includes cluster metadata, total vote points (sum of dots),
    representative idea text (when available), and up to three sample ideas.
    """

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_clusters",
            namespace="workshop",
            description=(
                "List the latest idea clusters for the current workshop, including "
                "name, description/gist, ideas_count, vote_points, representative idea, and sample ideas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workshop_id": {"type": "integer"},
                    # Optional: auto-injected by registry when requires_auth is True
                    # Included to avoid validation errors when additionalProperties is False
                    "user_id": {"type": "integer", "minimum": 1},
                },
                "required": [],  # auto-hydrated by registry when requires_workshop is True
                "additionalProperties": False,
            },
            returns={
                "type": "object",
                "properties": {
                    "clusters": {"type": "array"},
                },
            },
            requires_auth=True,
            requires_workshop=True,
        )

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        workshop_id = params.get("workshop_id")
        # Ensure workshop exists (raises ToolExecutionError if not)
        self.ensure_workshop(workshop_id)

        # Find the most recent clustering_voting task for this workshop
        task = (
            BrainstormTask.query
            .filter_by(workshop_id=workshop_id, task_type="clustering_voting")
            .order_by(BrainstormTask.created_at.desc())
            .first()
        )
        if not task:
            # No clustering results yet; return empty list gracefully
            return ToolResult(success=True, data={"clusters": []}, metadata={"reason": "no_clustering_task"})

        clusters_q = task.clusters.order_by(IdeaCluster.id.asc())
        clusters: List[IdeaCluster] = clusters_q.all()
        if not clusters:
            return ToolResult(success=True, data={"clusters": []})

        # Aggregate vote points per cluster in one query
        vote_rows = (
            db.session.query(IdeaVote.cluster_id, func.coalesce(func.sum(IdeaVote.dots_used), 0))
            .filter(IdeaVote.cluster_id.in_([c.id for c in clusters]))
            .group_by(IdeaVote.cluster_id)
            .all()
        )
        votes_map = {cid: int(total or 0) for cid, total in vote_rows}

        results: List[Dict[str, Any]] = []
        for c in clusters:
            # Sample up to three ideas (prefer corrected text)
            idea_rows: List[BrainstormIdea] = (
                c.ideas.order_by(BrainstormIdea.id.asc()).limit(3).all()  # type: ignore
            )
            samples = [
                {
                    "idea_id": idea.id,
                    "text": (idea.corrected_text or idea.content or "").strip(),
                }
                for idea in idea_rows
            ]

            rep_text = None
            if getattr(c, "representative_idea_id", None):
                rep = db.session.get(BrainstormIdea, c.representative_idea_id)
                if rep:
                    rep_text = (rep.corrected_text or rep.content or "").strip()

            # Count ideas efficiently when relationship is dynamic
            try:
                ideas_count = c.ideas.count()  # type: ignore[attr-defined]
            except Exception:
                # Fallback if relationship loader changed
                try:
                    ideas_count = len(list(c.ideas))  # type: ignore[arg-type]
                except Exception:
                    ideas_count = 0

            results.append(
                {
                    "cluster_id": c.id,
                    "name": c.name,
                    "description": (c.description or c.theme_gist or "").strip(),
                    "gist": (c.theme_gist or "").strip(),
                    "ideas_count": ideas_count,
                    "vote_points": votes_map.get(c.id, 0),
                    "representative_idea_id": c.representative_idea_id,
                    "representative_text": rep_text,
                    "samples": samples,
                }
            )

        return ToolResult(success=True, data={"clusters": results})
